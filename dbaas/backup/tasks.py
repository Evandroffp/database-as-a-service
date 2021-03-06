# -*- coding: utf-8 -*-
import logging
from dbaas.celery import app
from util.decorators import only_one
from physical.models import DatabaseInfra, Plan, Instance
from logical.models import Database
from models import Snapshot
from notification.models import TaskHistory
from system.models import Configuration
from drivers.base import ConnectionError
import datetime
import time
from datetime import date, timedelta
from util import exec_remote_command
from dbaas_cloudstack.models import HostAttr as Cloudstack_HostAttr
from util import get_worker_name
from util import build_dict
from util.providers import get_restore_snapshot_settings
from workflow.workflow import start_workflow
from notification import tasks
from workflow.steps.util.nfsaas_utils import create_snapshot, delete_snapshot, \
    delete_export

LOG = logging.getLogger(__name__)


def set_backup_error(databaseinfra, snapshot, errormsg):
    LOG.error(errormsg)
    snapshot.status = Snapshot.ERROR
    snapshot.error = errormsg
    snapshot.size = 0
    snapshot.end_at = datetime.datetime.now()
    snapshot.purge_at = datetime.datetime.now()
    snapshot.save()
    register_backup_dbmonitor(databaseinfra, snapshot)


def register_backup_dbmonitor(databaseinfra, snapshot):
    try:
        from dbaas_dbmonitor.provider import DBMonitorProvider
        DBMonitorProvider().register_backup(databaseinfra=databaseinfra,
                                            start_at=snapshot.start_at,
                                            end_at=snapshot.end_at,
                                            size=snapshot.size,
                                            status=snapshot.status,
                                            type=snapshot.type,
                                            error=snapshot.error)
    except Exception as e:
        LOG.error("Error register backup on DBMonitor %s" % (e))


def mysql_binlog_save(client, instance, cloudstack_hostattr):

    try:
        client.query('show master status')
        r = client.store_result()
        row = r.fetch_row(maxrows=0, how=1)
        binlog_file = row[0]['File']
        binlog_pos = row[0]['Position']

        client.query("show variables like 'datadir'")
        r = client.store_result()
        row = r.fetch_row(maxrows=0, how=1)
        datadir = row[0]['Value']

        output = {}
        command = 'echo "master=%s;position=%s" > %smysql_binlog_master_file_pos' % (
            binlog_file, binlog_pos, datadir)

        exec_remote_command(server=instance.hostname.address,
                            username=cloudstack_hostattr.vm_user,
                            password=cloudstack_hostattr.vm_password,
                            command=command,
                            output=output)
    except Exception as e:
        LOG.error(
            "Error saving mysql master binlog file and position: %s" % (e))


def lock_instance(driver, instance, client):
    try:
        LOG.debug('Locking instance {}'.format(instance))
        driver.lock_database(client)
        LOG.debug('Instance {} is locked'.format(instance))
        return True
    except Exception as e:
        LOG.warning('Could not lock {} - {}'.format(instance, e))
        return False


def unlock_instance(driver, instance, client):
    LOG.debug('Unlocking instance {}'.format(instance))
    driver.unlock_database(client)
    LOG.debug('Instance {} is unlocked'.format(instance))


def make_instance_snapshot_backup(instance, error):
    LOG.info("Make instance backup for %s" % (instance))

    snapshot = Snapshot()
    snapshot.start_at = datetime.datetime.now()
    snapshot.type = Snapshot.SNAPSHOPT
    snapshot.status = Snapshot.RUNNING
    snapshot.instance = instance
    snapshot.environment = instance.databaseinfra.environment

    from dbaas_nfsaas.models import HostAttr as Nfsaas_HostAttr
    nfsaas_hostattr = Nfsaas_HostAttr.objects.get(
        host=instance.hostname, is_active=True
    )
    snapshot.export_path = nfsaas_hostattr.nfsaas_path

    databases = Database.objects.filter(databaseinfra=instance.databaseinfra)
    if databases:
        snapshot.database_name = databases[0].name
    snapshot.save()

    snapshot_final_status = Snapshot.SUCCESS

    try:
        databaseinfra = instance.databaseinfra
        driver = databaseinfra.get_driver()
        client = driver.get_client(instance)
        cloudstack_hostattr = Cloudstack_HostAttr.objects.get(
            host=instance.hostname
        )

        locked = lock_instance(driver, instance, client)
        if not locked:
            snapshot_final_status = Snapshot.WARNING

        if type(driver).__name__ == 'MySQL':
            mysql_binlog_save(client, instance, cloudstack_hostattr)

        nfs_snapshot = create_snapshot(
            environment=databaseinfra.environment, host=instance.hostname
        )

        if 'id' in nfs_snapshot and 'name' in nfs_snapshot:
            snapshot.snapshopt_id = nfs_snapshot['id']
            snapshot.snapshot_name = nfs_snapshot['name']
        else:
            errormsg = 'There is no snapshot information'
            error['errormsg'] = errormsg
            set_backup_error(databaseinfra, snapshot, errormsg)
            return snapshot

    except Exception as e:
        errormsg = "Error creating snapshot: %s" % (e)
        error['errormsg'] = errormsg
        set_backup_error(databaseinfra, snapshot, errormsg)
        return snapshot
    finally:
        if locked:
            unlock_instance(driver, instance, client)

    output = {}
    command = "du -sb /data/.snapshot/%s | awk '{print $1}'" % (
        snapshot.snapshot_name)
    try:
        exec_remote_command(server=instance.hostname.address,
                            username=cloudstack_hostattr.vm_user,
                            password=cloudstack_hostattr.vm_password,
                            command=command,
                            output=output)
        size = int(output['stdout'][0])
        snapshot.size = size
    except Exception as e:
        snapshot.size = 0
        LOG.error("Error exec remote command %s" % (e))

    backup_path = databases[0].backup_path
    if backup_path:
        infraname = databaseinfra.name
        now = datetime.datetime.now()
        target_path = "{backup_path}/{today_str}/{hostname}/{now_str}/{infraname}".format(
            backup_path=backup_path,
            today_str=now.strftime("%Y_%m_%d"),
            hostname=instance.hostname.hostname.split('.')[0],
            now_str=now.strftime("%Y%m%d%H%M%S"),
            infraname=infraname)
        snapshot_path = "/data/.snapshot/{}/data/".format(snapshot.snapshot_name)
        output = {}
        command = """
        if [ -d "{backup_path}" ]
        then
            rm -rf {backup_path}/20[0-9][0-9]_[0-1][0-9]_[0-3][0-9] &
            mkdir -p {target_path}
            cp -r {snapshot_path} {target_path} &
        fi
        """.format(backup_path=backup_path,
                   target_path=target_path,
                   snapshot_path=snapshot_path)
        try:
            exec_remote_command(server=instance.hostname.address,
                                username=cloudstack_hostattr.vm_user,
                                password=cloudstack_hostattr.vm_password,
                                command=command,
                                output=output)
        except Exception as e:
            LOG.error("Error exec remote command %s" % (e))

    snapshot.status = snapshot_final_status
    snapshot.end_at = datetime.datetime.now()
    snapshot.save()
    register_backup_dbmonitor(databaseinfra, snapshot)

    return snapshot


@app.task(bind=True)
@only_one(key="makedatabasebackupkey")
def make_databases_backup(self):

    LOG.info("Making databases backups")
    worker_name = get_worker_name()
    task_history = TaskHistory.register(request=self.request,
                                        worker_name=worker_name, user=None)

    status = TaskHistory.STATUS_SUCCESS
    databaseinfras = DatabaseInfra.objects.filter(
        plan__provider=Plan.CLOUDSTACK, plan__has_persistence=True
    )
    error = {}
    backup_number = 0
    backups_per_group = len(databaseinfras) / 12
    for databaseinfra in databaseinfras:
        if backups_per_group > 0:
            if backup_number < backups_per_group:
                backup_number += 1
            else:
                backup_number = 0
                waiting_msg = "\nWaiting 5 minutes to start the next backup group"
                task_history.update_details(persist=True, details=waiting_msg)
                time.sleep(300)

        instances = Instance.objects.filter(
            databaseinfra=databaseinfra, read_only=False
        )
        for instance in instances:
            try:
                if not instance.databaseinfra.get_driver().check_instance_is_eligible_for_backup(instance):
                    LOG.info('Instance %s is not eligible for backup' % (str(instance)))
                    continue
            except Exception as e:
                status = TaskHistory.STATUS_ERROR
                msg = "Backup for %s was unsuccessful. Error: %s" % (
                    str(instance), str(e))
                LOG.error(msg)
            else:
                time_now = str(time.strftime("%m/%d/%Y %H:%M:%S"))
                start_msg = "\n{} - Starting backup for {} ...".format(time_now, instance)
                task_history.update_details(persist=True, details=start_msg)
                try:
                    snapshot = make_instance_snapshot_backup(
                        instance=instance, error=error
                    )
                    if snapshot and snapshot.was_successful:
                        msg = "Backup for %s was successful" % (str(instance))
                        LOG.info(msg)
                    elif snapshot and snapshot.has_warning:
                        status = TaskHistory.STATUS_WARNING
                        msg = "Backup for %s has warning" % (str(instance))
                        LOG.info(msg)
                    else:
                        status = TaskHistory.STATUS_ERROR
                        msg = "Backup for %s was unsuccessful. Error: %s" % (
                            str(instance), error['errormsg'])
                        LOG.error(msg)
                    LOG.info(msg)
                except Exception as e:
                    status = TaskHistory.STATUS_ERROR
                    msg = "Backup for %s was unsuccessful. Error: %s" % (
                        str(instance), str(e))
                    LOG.error(msg)

            time_now = str(time.strftime("%m/%d/%Y %H:%M:%S"))
            msg = "\n{} - {}".format(time_now, msg)
            task_history.update_details(persist=True, details=msg)

    task_history.update_status_for(status, details="\nBackup finished")

    return


def remove_snapshot_backup(snapshot):
    LOG.info("Removing backup for %s" % (snapshot))

    delete_snapshot(snapshot)

    snapshot.purge_at = datetime.datetime.now()
    snapshot.save()
    return


@app.task(bind=True)
@only_one(key="removedatabaseoldbackupkey")
def remove_database_old_backups(self):

    worker_name = get_worker_name()
    task_history = TaskHistory.register(request=self.request,
                                        worker_name=worker_name, user=None)

    backup_retention_days = Configuration.get_by_name_as_int(
        'backup_retention_days')

    LOG.info("Removing backups older than %s days" % (backup_retention_days))

    backup_time_dt = date.today() - timedelta(days=backup_retention_days)
    snapshots = Snapshot.objects.filter(start_at__lte=backup_time_dt,
                                        purge_at__isnull=True,
                                        instance__isnull=False,
                                        snapshopt_id__isnull=False)
    msgs = []
    status = TaskHistory.STATUS_SUCCESS
    if len(snapshots) == 0:
        msgs.append("There is no snapshot to purge")
    for snapshot in snapshots:
        try:
            remove_snapshot_backup(snapshot=snapshot)
            msg = "Backup %s removed" % (snapshot)
            LOG.info(msg)
        except Exception as e:
            msg = "Error removing backup %s. Error: %s" % (snapshot, str(e))
            status = TaskHistory.STATUS_ERROR
            LOG.error(msg)
        msgs.append(msg)

    task_history.update_status_for(status, details="\n".join(msgs))

    return


@app.task(bind=True)
def restore_snapshot(self, database, snapshot, user, task_history):
    try:
        from dbaas_nfsaas.models import HostAttr
        LOG.info("Restoring snapshot")
        worker_name = get_worker_name()

        # task_history = models.TaskHistory.objects.get(id=task_history)
        task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                            user=user, worker_name=worker_name)

        databaseinfra = database.databaseinfra

        snapshot = Snapshot.objects.get(id=snapshot)
        snapshot_id = snapshot.snapshopt_id

        host_attr_snapshot = HostAttr.objects.get(nfsaas_path=snapshot.export_path)
        host = host_attr_snapshot.host
        host_attr = HostAttr.objects.get(host=host, is_active=True)

        export_id_snapshot = host_attr_snapshot.nfsaas_export_id
        export_id = host_attr.nfsaas_export_id
        export_path = host_attr.nfsaas_path

        steps = get_restore_snapshot_settings(
            database.plan.replication_topology.class_path
        )

        not_primary_instances = databaseinfra.instances.exclude(
            hostname=host
        ).exclude(instance_type__in=[
            Instance.MONGODB_ARBITER, Instance.REDIS_SENTINEL
        ])
        not_primary_hosts = [
            arbiter.hostname for arbiter in databaseinfra.instances.filter(
                instance_type=Instance.MONGODB_ARBITER
            )
        ]
        for instance in not_primary_instances:
            not_primary_hosts.append(instance.hostname)

        workflow_dict = build_dict(databaseinfra=databaseinfra,
                                   database=database,
                                   snapshot_id=snapshot_id,
                                   export_path=export_path,
                                   export_id=export_id,
                                   export_id_snapshot=export_id_snapshot,
                                   host=host,
                                   steps=steps,
                                   not_primary_hosts=not_primary_hosts,
                                   )

        start_workflow(workflow_dict=workflow_dict, task=task_history)

        if workflow_dict['exceptions']['traceback']:
            raise Exception('Restore could not be finished')
        else:
            task_history.update_status_for(
                TaskHistory.STATUS_SUCCESS, details='Database sucessfully recovered!')

    except Exception, e:
        if 'workflow_dict' in locals():
            error = "\n".join(": ".join(err) for err in
                              workflow_dict['exceptions']['error_codes'])
            traceback = "\nException Traceback\n".join(workflow_dict['exceptions']['traceback'])
            error = "{}\n{}\n{}".format(error, traceback, error)
        else:
            error = str(e)
        task_history.update_status_for(
            TaskHistory.STATUS_ERROR, details=error)
    finally:
        tasks.enable_zabbix_alarms(database)


@app.task(bind=True)
@only_one(key="purge_unused_exports")
def purge_unused_exports_task(self):
    from notification.tasks import TaskRegister
    task = TaskRegister.purge_unused_exports()

    task = TaskHistory.register(
        request=self.request, worker_name=get_worker_name(), task_history=task
    )

    task.add_detail('Getting all inactive exports without snapshots')
    if purge_unused_exports(task):
        task.set_status_success('Done')
    else:
        task.set_status_error('Error')


def purge_unused_exports(task=None):
    from dbaas_nfsaas.models import HostAttr

    success = True
    for export in HostAttr.objects.filter(is_active=False):
        if export.snapshots():
            continue

        if task:
            task.add_detail('Removing: {}'.format(export), level=2)

        environment = export.host.instances.first().databaseinfra.environment

        try:
            delete_export(environment, export.nfsaas_path_host)
        except Exception as e:
            success = False
            LOG.info('Error removing {} - {}'.format(export, e))
            if task:
                task.add_detail('Error: {}'.format(e), level=4)
        else:
            if task:
                task.add_detail('Success', level=4)
            export.delete()

    return success


def _get_backup_instance(database, task):
    task.add_detail('Searching for backup eligible instance...')

    driver = database.infra.get_driver()
    instances = database.infra.instances.filter(read_only=False)
    for instance in instances:
        try:
            task.add_detail('Instance: {}'.format(instance), level=1)
            if driver.check_instance_is_eligible_for_backup(instance):
                task.add_detail('Is Eligible', level=2)
                return instance

            task.add_detail('Not eligible', level=2)
        except ConnectionError:
            task.add_detail('Connection error', level=2)
            continue

    task.add_detail('No instance eligible for backup', level=1)
    return


def _check_snapshot_limit(instance, task):
    task.add_detail('\nChecking older backups...')

    backup_limit = Configuration.get_by_name_as_int('backup_retention_days')
    snapshots_count = Snapshot.objects.filter(
        purge_at__isnull=True, instance=instance, snapshopt_id__isnull=False
    ).order_by('start_at').count()
    task.add_detail(
        'Current snapshot limit {}, used {}'.format(
            backup_limit, snapshots_count
        ),
        level=1
    )


def _create_database_backup(instance, task):
    task.add_detail('\nStarting backup for {}...'.format(instance))
    error = {}
    try:
        snapshot = make_instance_snapshot_backup(
            instance=instance, error=error
        )
    except Exception as e:
        task.add_detail('\nError: {}'.format(e))
        return False
    else:
        if 'errormsg' in error:
            task.add_detail('\nError: {}'.format(error['errormsg']))
            return False
        return snapshot


@app.task(bind=True)
def make_database_backup(self, database, task):
    worker_name = get_worker_name()
    task_history = TaskHistory.register(
        request=self.request, worker_name=worker_name, task_history=task
    )

    if not database.pin_task(task):
        task.error_in_lock(database)
        return False

    task_history.add_detail('Starting database {} backup'.format(database))

    instance = _get_backup_instance(database, task)
    if not instance:
        task.set_status_error('Could not find eligible instance', database)
        return False

    _check_snapshot_limit(instance, task)

    snapshot = _create_database_backup(instance, task)
    if not snapshot:
        task.set_status_error('Backup was unsuccessful', database)
        return False

    snapshot.is_automatic = False
    snapshot.save()

    if snapshot.was_successful:
        task.set_status_success('Backup was successful', database)
    elif snapshot.has_warning:
        task.set_status_warning('Backup was warning', database)
    return True


@app.task(bind=True)
def remove_database_backup(self, task, snapshot):
    worker_name = get_worker_name()
    task_history = TaskHistory.register(
        request=self.request, worker_name=worker_name, task_history=task
    )

    task_history.add_detail('Removing {}'.format(snapshot))
    try:
        remove_snapshot_backup(snapshot)
    except Exception as e:
        task_history.add_detail('Error: {}'.format(e))
        task.set_status_error('Could not delete backup')
        return False
    else:
        task.set_status_success('Backup deleted with success')
        return True
