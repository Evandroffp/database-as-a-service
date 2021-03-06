# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals
import simple_audit
import logging
import datetime
from django.db import models, transaction, Error
from django.db.models.signals import pre_save, post_save, pre_delete
from django.contrib.auth.models import User
from django.dispatch import receiver
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.translation import ugettext_lazy as _
from django_extensions.db.fields.encrypted import EncryptedCharField
from util import slugify, make_db_random_password
from util.models import BaseModel
from physical.models import DatabaseInfra, Environment
from drivers import factory_for
from system.models import Configuration
from datetime import date, timedelta
from account.models import Team
from drivers.base import ConnectionError, DatabaseStatus
from logical.validators import database_name_evironment_constraint
from notification.models import TaskHistory

LOG = logging.getLogger(__name__)
KB_FACTOR = 1.0 / 1024.0
MB_FACTOR = 1.0 / 1024.0 / 1024.0
GB_FACTOR = 1.0 / 1024.0 / 1024.0 / 1024.0


class Project(BaseModel):
    name = models.CharField(
        verbose_name=_("Project name"), max_length=100, unique=True)
    description = models.TextField(
        verbose_name=_("Description"), null=True, blank=True)
    is_active = models.BooleanField(
        verbose_name=_("Is project active"), default=True)
    slug = models.SlugField()

    def __unicode__(self):
        return "%s" % self.name

    class Meta:
        permissions = (
            ("view_project", "Can view projects"),
        )
        ordering = ['name']


class DatabaseAliveManager(models.Manager):

    def get_query_set(self):
        return Database.objects.filter(is_in_quarantine=False)


class Database(BaseModel):
    DEAD = 0
    ALIVE = 1
    INITIALIZING = 2
    ALERT = 3

    DB_STATUS = (
        (DEAD, 'Dead'),
        (ALIVE, 'Alive'),
        (INITIALIZING, 'Initializing'),
        (ALERT, 'Alert')
    )

    name = models.CharField(
        verbose_name=_("Database name"), max_length=100, db_index=True
    )
    databaseinfra = models.ForeignKey(
        DatabaseInfra, related_name="databases", on_delete=models.PROTECT
    )
    project = models.ForeignKey(
        Project, related_name="databases", on_delete=models.PROTECT, null=True,
        blank=True
    )
    team = models.ForeignKey(
        Team, related_name="databases", null=True, blank=True,
        help_text=_("Team that is accountable for the database")
    )
    is_in_quarantine = models.BooleanField(
        verbose_name=_("Is database in quarantine?"), default=False
    )
    quarantine_dt = models.DateField(
        verbose_name=_("Quarantine date"), null=True, blank=True,
        editable=False
    )
    description = models.TextField(
        verbose_name=_("Description"), null=True, blank=True
    )
    status = models.IntegerField(choices=DB_STATUS, default=2)
    used_size_in_bytes = models.FloatField(default=0.0)
    environment = models.ForeignKey(
        Environment, related_name="databases", on_delete=models.PROTECT,
        db_index=True
    )
    backup_path = models.CharField(
        verbose_name=_("Backup path"), max_length=300, null=True, blank=True,
        help_text=_("Full path to backup file")
    )
    subscribe_to_email_events = models.BooleanField(
        verbose_name=_("Subscribe to email events"), default=True,
        help_text=_(
            "Check this box if you'd like to receive information "
            "regarding this database by email."
        )
    )
    disk_auto_resize = models.BooleanField(
        verbose_name=_("Disk auto resize"), default=True,
        help_text=_("When marked, the disk will be resized automatically.")
    )
    is_protected = models.BooleanField(
        verbose_name=_("Protected"), default=False,
        help_text=_("When marked, the database can not be deleted.")
    )
    quarantine_user = models.ForeignKey(
        User, related_name='databases_quarantine',
        null=True, blank=True, editable=False
    )

    def team_contact(self):
        if self.team:
            return self.team.emergency_contacts
    team_contact.short_description = 'Emergency contacts'

    objects = models.Manager()
    alive = DatabaseAliveManager()
    quarantine_time = Configuration.get_by_name_as_int(
        'quarantine_retention_days'
    )

    def __unicode__(self):
        return u"{}".format(self.name)

    class Meta:
        permissions = (
            ("can_manage_quarantine_databases", "Can manage databases in quarantine"),
            ("view_database", "Can view databases"),
            ("upgrade_mongo24_to_30", "Can upgrade mongoDB version from 2.4 to 3.0"),
            ("upgrade_database", "Can upgrade databases"),
        )
        unique_together = (
            ('name', 'environment'),
        )

        ordering = ('name', )

    @property
    def infra(self):
        return self.databaseinfra

    @property
    def engine_type(self):
        return self.infra.engine_name

    @property
    def engine(self):
        return self.infra.engine

    @property
    def plan(self):
        return self.databaseinfra and self.databaseinfra.plan

    def pin_task(self, task):
        try:
            with transaction.atomic():
                DatabaseLock(database=self, task=task).save()
        except Error:
            return False
        else:
            return True

    def update_task(self, task):
        lock = self.lock.first()
        if not lock:
            return self.pin_task(task)

        with transaction.atomic():
            lock = DatabaseLock.objects.select_for_update().filter(
                database=self
            ).first()
            if lock.task.task_name != task.task_name or not lock.task.is_status_error:
                return False

            lock.task = task
            lock.save()
            return True

    def unpin_task(self):
        DatabaseLock.objects.filter(database=self).delete()

    @property
    def current_locked_task(self):
        lock = self.lock.first()
        if lock:
            return lock.task

    def delete(self, *args, **kwargs):
        if self.is_in_quarantine:
            LOG.warning(
                "Database {} is in quarantine and will be removed".format(
                    self.name
                )
            )
            for credential in self.credentials.all():
                instance = factory_for(self.databaseinfra)
                instance.remove_user(credential)
            super(Database, self).delete(*args, **kwargs)

        else:
            LOG.warning("Putting database {} in quarantine".format(self.name))
            self.is_in_quarantine = True
            self.is_protected = False

            self.save()
            if self.credentials.exists():
                for credential in self.credentials.all():
                    new_password = make_db_random_password()
                    new_credential = Credential.objects.get(pk=credential.id)
                    new_credential.password = new_password
                    new_credential.save()

                    instance = factory_for(self.databaseinfra)
                    instance.update_user(new_credential)

    def clean(self):
        if not self.pk:
            self.name = slugify(self.name)

        if self.name in self.__get_database_reserved_names():
            raise ValidationError(
                _("{} is a reserved database name".format(
                    self.name
                ))
            )

    def automatic_create_first_credential(self):
        LOG.info("creating new credential for database {}".format(self.name))
        user = Credential.USER_PATTERN % self.name
        credential = Credential.create_new_credential(user, self)
        return credential

    @classmethod
    def provision(cls, name, databaseinfra):
        if not isinstance(databaseinfra, DatabaseInfra):
            raise ValidationError(
                'Invalid databaseinfra type {} - {}'.format(
                    type(databaseinfra), databaseinfra
                )
            )

        database = Database()
        database.databaseinfra = databaseinfra
        database.environment = databaseinfra.environment
        database.name = name
        database.full_clean()
        database.save()
        database = Database.objects.get(pk=database.pk)
        return database

    def __get_database_reserved_names(self):
        return getattr(self.driver, 'RESERVED_DATABASES_NAME', [])

    @property
    def driver(self):
        if self.databaseinfra_id is not None:
            return self.databaseinfra.get_driver()

    def get_endpoint(self):
        return self.driver.get_connection(database=self)

    def get_endpoint_dns(self):
        return self.driver.get_connection_dns(database=self)

    def get_endpoint_dns_simple(self):
        return self.driver.get_connection_dns_simple(database=self)

    def __graylog_url(self):
        from util import get_credentials_for
        from dbaas_credentials.models import CredentialType

        if self.databaseinfra.plan.is_pre_provisioned:
            return ""

        credential = get_credentials_for(
            environment=self.environment,
            credential_type=CredentialType.GRAYLOG
        )
        stream = credential.get_parameter_by_name(
            'stream_{}'.format(self.plan.engine.engine_type.name)
        )
        search_field = credential.get_parameter_by_name('search_field')
        if not stream or not search_field:
            return ""

        return "{}/streams/{}/search?q={}:{}".format(
            credential.endpoint, stream, search_field, self.name
        )

    def get_log_url(self):
        if Configuration.get_by_name_as_int('graylog_integration') == 1:
            return self.__graylog_url()

    def get_dex_url(self):
        if Configuration.get_by_name_as_int('dex_analyze') != 1:
            return ""

        if self.databaseinfra.plan.is_pre_provisioned:
            return ""

        if self.engine_type != 'mongodb':
            return ""

        return 1

    def get_is_preprovisioned(self):
        return self.databaseinfra.plan.is_pre_provisioned

    endpoint = property(get_endpoint)
    endpoint_dns = property(get_endpoint_dns)

    @cached_property
    def database_status(self):
        try:
            info = self.databaseinfra.get_info()
            if info is None:
                return None
            database_status = info.get_database_status(self.name)

            if database_status is None:
                # try get without cache
                info = self.databaseinfra.get_info(force_refresh=True)
                database_status = info.get_database_status(self.name)
        except ConnectionError as e:
            msg = "ConnectionError calling database_status for database {}: {}".format(self, e)
            LOG.error(msg)
            database_status = DatabaseStatus(self)

        return database_status

    def get_cloudstack_service_offering(self):
        LOG.info("Get offering")
        try:
            offer_name = self.databaseinfra.cs_dbinfra_offering.get(
            ).offering.name
        except Exception as e:
            LOG.info("Oops...{}".format(e))
            offer_name = None

        return offer_name

    offering = property(get_cloudstack_service_offering)

    @property
    def total_size(self):
        return self.databaseinfra.per_database_size_bytes

    @property
    def total_size_in_kb(self):
        return round(self.databaseinfra.per_database_size_bytes * KB_FACTOR, 2)

    @property
    def total_size_in_mb(self):
        return round(self.databaseinfra.per_database_size_bytes * MB_FACTOR, 2)

    @property
    def total_size_in_gb(self):
        return round(self.databaseinfra.per_database_size_bytes * GB_FACTOR, 2)

    @property
    def used_size_in_kb(self):
        return self.used_size_in_bytes * KB_FACTOR

    @property
    def used_size_in_mb(self):
        return self.used_size_in_bytes * MB_FACTOR

    @property
    def used_size_in_gb(self):
        return self.used_size_in_bytes * GB_FACTOR

    @property
    def capacity(self):
        if self.status:
            return round((1.0 * self.used_size_in_bytes / self.total_size) if self.total_size else 0, 2)

    @classmethod
    def purge_quarantine(self):
        quarantine_time = Configuration.get_by_name_as_int(
            'quarantine_retention_days')
        quarantine_time_dt = date.today() - timedelta(days=quarantine_time)
        databases = Database.objects.filter(
            is_in_quarantine=True, quarantine_dt__lte=quarantine_time_dt
        )
        for database in databases:
            database.delete()
            LOG.info("The database %s was deleted, because it was set to quarentine %d days ago" % (
                database.name, quarantine_time)
            )

    @classmethod
    def clone(cls, database, clone_name, plan, environment, user):
        from notification.tasks import TaskRegister

        TaskRegister.database_clone(
            origin_database=database, clone_name=clone_name, plan=plan,
            environment=environment, user=user
        )

    @classmethod
    def restore(cls, database, snapshot, user):
        from notification.tasks import TaskRegister

        LOG.info(
            "Changing database volume with params: database {} snapshot: {}, user: {}".format(
                database, snapshot, user
            )
        )
        TaskRegister.restore_snapshot(
            database=database, snapshot=snapshot, user=user
        )

    @classmethod
    def resize(cls, database, cloudstackpack, user):
        from notification.tasks import TaskRegister

        TaskRegister.database_resize(
            database=database, user=user,
            cloudstack_pack=cloudstackpack
        )

#    @classmethod
#    def recover_snapshot(cls, database, snapshot, user, task_history):
#        from backup.tasks import restore_snapshot
#
#        restore_snapshot.delay(
#            database=database, snapshot=snapshot, user=user,
#            task_history=task_history
#        )

    def get_metrics_url(self):
        return "/admin/logical/database/{}/metrics/".format(self.id)

    def get_resize_retry_url(self):
        return "/admin/logical/database/{}/resize_retry/".format(self.id)

    def get_disk_resize_url(self):
        return "/admin/logical/database/{}/disk_resize/".format(self.id)

    def get_mongodb_engine_version_upgrade_url(self):
        return "/admin/logical/database/{}/mongodb_engine_version_upgrade/".format(self.id)

    def get_upgrade_url(self):
        return "/admin/logical/database/{}/upgrade/".format(self.id)

    def get_upgrade_retry_url(self):
        return "/admin/logical/database/{}/upgrade_retry/".format(self.id)

    def get_change_parameters_retry_url(self):
        return "/admin/logical/database/{}/change_parameters_retry/".format(self.id)

    def is_mongodb_24(self):
        engine = self.engine
        if engine.name == 'mongodb' and engine.version.startswith('2.4'):
            return True
        return False

    def get_cloudstack_service_offering_id(self):
        LOG.info("Get offering")
        try:
            offer_id = self.databaseinfra.cs_dbinfra_offering.get(
            ).offering.serviceofferingid
        except Exception as e:
            LOG.info("Oops...{}".format(e))
            offer_id = None

        return offer_id

    offering_id = property(get_cloudstack_service_offering_id)

    def is_being_used_elsewhere(self, skip_task_name=None):
        tasks = TaskHistory.objects.filter(
            task_status=TaskHistory.STATUS_WAITING,
            object_id=self.id,
            object_class=self._meta.db_table)

        if tasks:
            return True
        if not self.current_locked_task:
            return False

        if self.current_locked_task.task_name == skip_task_name:
            if self.current_locked_task.is_status_error:
                return False

        return True

    def restore_allowed(self):
        if Configuration.get_by_name_as_int('restore_allowed') == 1:
            return True

        return False

    def has_cloudstack_offerings(self):
        from dbaas_cloudstack.models import CloudStackPack

        offerings = CloudStackPack.objects.filter(
            offering__region__environment=self.environment,
            engine_type__name=self.engine_type
        ).exclude(offering__serviceofferingid=self.offering_id)

        return bool(offerings)

    def has_disk_offerings(self):
        from physical.models import DiskOffering

        offerings = DiskOffering.objects.exclude(
            id=self.databaseinfra.disk_offering.id
        )
        return bool(offerings)

    @property
    def can_modify_parameters(self):
        if self.plan.replication_topology.parameter.all():
            return True
        else:
            return False

    @property
    def is_dead(self):
        if self.status != Database.ALIVE:
            return True

        if self.database_status and not self.database_status.is_alive:
            return True

        return False

    @classmethod
    def disk_resize(cls, database, new_disk_offering, user):
        from physical.models import DiskOffering
        from notification.tasks import TaskRegister

        disk_offering = DiskOffering.objects.get(id=new_disk_offering)

        TaskRegister.database_disk_resize(database=database, user=user, disk_offering=disk_offering)

    def update_host_disk_used_size(self, host_address, used_size_kb, total_size_kb=None):
        instance = self.databaseinfra.instances.filter(address=host_address).first()
        if not instance:
            raise ObjectDoesNotExist()

        nfsaas_host = instance.hostname.nfsaas_host_attributes.last()
        if not nfsaas_host:
            return None

        if total_size_kb:
            nfsaas_host.nfsaas_size_kb = total_size_kb

        nfsaas_host.nfsaas_used_size_kb = used_size_kb
        nfsaas_host.save()
        return nfsaas_host

    def can_be_cloned(self, database_view_button=False):
        if not self.plan.has_persistence:
            return False, "Database does not have persistence cannot be cloned"

        if self.is_being_used_elsewhere():
            return False, "Database is being used by another task"

        if self.is_in_quarantine:
            return False, "Database in quarantine cannot be cloned"

        if database_view_button:
            if self.status != self.ALIVE:
                return False, "Database is not alive and cannot be cloned"
        else:
            if self.is_dead:
                return False, "Database is not alive and cannot be cloned"

        return True, None

    def can_be_restored(self):
        if not self.restore_allowed():
            return False, 'Restore is not allowed. Please, contact DBaaS team for more information'

        if self.is_in_quarantine:
            return False, "Database in quarantine cannot be restored"

        if self.status != self.ALIVE or self.is_dead:
            return False, "Database is not alive and cannot be restored"

        if self.is_being_used_elsewhere():
            return False, "Database is being used by another task, please check your tasks"

        return True, None

    def can_be_deleted(self):
        error = None
        if self.is_protected and not self.is_in_quarantine:
            error = "Database {} is protected and cannot be deleted"
        elif self.is_dead:
            error = "Database {} is not alive and cannot be deleted"
        elif self.is_being_used_elsewhere():
            error = "Database {} cannot be deleted because" \
                    " it is in use by another task."

        if error:
            return False, error.format(self.name)
        return True, None

    def can_do_upgrade_retry(self):
        error = None
        if self.is_mongodb_24():
            error = "MongoDB 2.4 cannot be upgraded by this task."
        elif self.is_in_quarantine:
            error = "Database in quarantine and cannot be upgraded."
        elif self.is_being_used_elsewhere('notification.tasks.upgrade_database'):
            error = "Database cannot be upgraded because " \
                    "it is in use by another task."
        elif not self.infra.plan.engine_equivalent_plan:
            error = "Source plan do not has equivalent plan to upgrade."

        if error:
            return False, error
        return True, None

    def can_do_upgrade(self):
        can_do_upgrade, error = self.can_do_upgrade_retry()

        if can_do_upgrade:
            if self.is_dead:
                error = "Database is dead and cannot be upgraded."
            elif self.is_being_used_elsewhere():
                error = "Database cannot be upgraded because " \
                        "it is in use by another task."

        if error:
            return False, error
        return True, None

    def can_do_resize_retry(self):
        error = None
        if self.is_in_quarantine:
            error = "Database in quarantine and cannot be resized."
        elif not self.has_cloudstack_offerings:
            error = "There is no offerings for this database."
        elif self.is_being_used_elsewhere('notification.tasks.resize_database'):
            error = "Database cannot be resized because" \
                    " it is in use by another task."
        if error:
            return False, error
        return True, None

    def can_do_resize(self):
        error = None
        if self.is_in_quarantine:
            error = "Database in quarantine and cannot be resized."
        elif not self.has_cloudstack_offerings:
            error = "There is no offerings for this database."
        elif self.is_dead:
            error = "Database is dead and cannot be resized."
        elif self.is_being_used_elsewhere():
            error = "Database cannot be resized because" \
                    " it is in use by another task."

        if error:
            return False, error
        return True, None

    def can_do_change_parameters_retry(self):
        error = None
        if self.is_in_quarantine:
            error = "Database in quarantine and cannot have the parameters changed."
        elif self.is_being_used_elsewhere('notification.tasks.change_parameters_database'):
            error = "Database cannot have the parameters changed because" \
                    " it is in use by another task."
        if error:
            return False, error
        return True, None

    def can_do_change_parameters(self):
        error = None
        if self.is_in_quarantine:
            error = "Database in quarantine and cannot have the parameters changed."
        elif self.is_dead:
            error = "Database is dead and cannot be resized."
        elif self.is_being_used_elsewhere():
            error = "Database cannot have the parameters changed because" \
                    " it is in use by another task."

        if error:
            return False, error
        return True, None

    def can_do_disk_resize(self):
        error = None
        if self.is_in_quarantine:
            error = "Database in quarantine and cannot be resized."
        elif self.is_being_used_elsewhere():
            error = "Database cannot be resized because" \
                    " it is in use by another task."
        elif not self.has_disk_offerings:
            error = "There is no other disk offering for this database."

        if error:
            return False, error
        return True, None

    def destroy(self, user):
        if not self.is_in_quarantine:
            self.delete()
            return

        if self.plan.provider != self.plan.CLOUDSTACK:
            self.delete()
            return

        LOG.debug(
            "call destroy_database - name={}, team={}, project={}, "
            "user={}".format(self.name, self.team, self.project, user)
        )

        from notification.tasks import TaskRegister

        TaskRegister.database_destroy(database=self, user=user)
        return

    @property
    def last_successful_upgrade(self):
        from maintenance.models import DatabaseUpgrade
        return self.upgrades.filter(status=DatabaseUpgrade.SUCCESS).last()

    @property
    def status_html(self):
        html_default = '<span class="label label-{}">{}</span>'

        if self.status == Database.ALIVE:
            status = html_default.format("success", "Alive")
        elif self.status == Database.DEAD:
            status = html_default.format("important", "Dead")
        elif self.status == Database.ALERT:
            status = html_default.format("warning", "Alert")
        else:
            status = html_default.format("info", "Initializing")

        return format_html(status)


class DatabaseLock(BaseModel):
    database = models.ForeignKey(
        Database, related_name="lock", unique=True
    )
    task = models.ForeignKey(
        TaskHistory, related_name="lock"
    )


class Credential(BaseModel):
    USER_PATTERN = "u_%s"
    USER_MAXIMUM_LENGTH_NAME = 16

    user = models.CharField(verbose_name=_("User name"), max_length=100)
    password = EncryptedCharField(
        verbose_name=_("User password"), max_length=255)
    database = models.ForeignKey(Database, related_name="credentials")

    def __unicode__(self):
        return u"%s" % self.user

    class Meta:
        permissions = (
            ("view_credential", "Can view credentials"),
        )
        unique_together = (
            ('user', 'database'),
        )
        ordering = ('database', 'user',)

    def clean(self):
        if len(self.user) > self.USER_MAXIMUM_LENGTH_NAME:
            raise ValidationError(_("%s is too long" % self.user))

    @cached_property
    def driver(self):
        return self.database.databaseinfra.get_driver()

    def reset_password(self):
        """ Reset credential password to a new random password """
        self.password = make_db_random_password()
        self.driver.update_user(self)
        self.save()

    @classmethod
    def create_new_credential(cls, user, database):
        credential = Credential()
        credential.database = database
        credential.user = user[:cls.USER_MAXIMUM_LENGTH_NAME]
        credential.user = slugify(credential.user)
        credential.password = make_db_random_password()
        credential.full_clean()
        credential.driver.create_user(credential)
        credential.save()
        return credential

    def delete(self, *args, **kwargs):
        self.driver.remove_user(self)
        LOG.info('User removed from driver')
        super(Credential, self).delete(*args, **kwargs)


#
# SIGNALS
#
@receiver(pre_delete, sender=Database)
def database_pre_delete(sender, **kwargs):
    """
database pre delete signal. Removes database from the engine
"""
    database = kwargs.get("instance")
    LOG.debug("database pre-delete triggered")
    engine = factory_for(database.databaseinfra)
    engine.remove_database(database)


@receiver(post_save, sender=Database)
def database_post_save(sender, **kwargs):
    """
database post save signal. Creates the database in the driver and creates a new credential.
"""
    database = kwargs.get("instance")
    is_new = kwargs.get("created")
    LOG.debug("database post-save triggered")
    if is_new and database.engine_type != 'redis':
        LOG.info("a new database (%s) were created... provision it in the engine" % (
            database.name))
        engine = factory_for(database.databaseinfra)
        engine.create_database(database)
        database.automatic_create_first_credential()


@receiver(pre_save, sender=Database)
def database_pre_save(sender, **kwargs):
    database = kwargs.get('instance')
    if database.is_in_quarantine:
        if database.quarantine_dt is None:
            database.quarantine_dt = datetime.datetime.now().date()

        if not database.quarantine_user:
            from dbaas.middleware import UserMiddleware
            database.quarantine_user = UserMiddleware.current_user()
    else:
        database.quarantine_dt = None
        database.quarantine_user = None

    if database.id:
        saved_object = Database.objects.get(id=database.id)
        if database.name != saved_object.name:
            raise AttributeError(_("Attribute name cannot be edited"))
    else:
        # new database
        if database_name_evironment_constraint(
           database.name, database.environment.name):
            raise AttributeError(
                _('%s already exists in production!') % database.name
            )

        LOG.debug("slugfying database's name for %s" % database.name)
        database.name = slugify(database.name)


@receiver(pre_save, sender=Credential)
def credential_pre_save(sender, **kwargs):
    credential = kwargs.get('instance')

    if credential.id:
        saved_object = Credential.objects.get(id=credential.id)
        if credential.user != saved_object.user:
            raise AttributeError(_("Attribute user cannot be edited"))

        if credential.database != saved_object.database:
            raise AttributeError(_("Attribute database cannot be edited"))


@receiver(pre_save, sender=Project)
def project_pre_save(sender, **kwargs):
    instance = kwargs.get('instance')
    instance.slug = slugify(instance.name)


class NoDatabaseInfraCapacity(Exception):

    """ There isn't databaseinfra capable to support a new database with this plan """
    pass


simple_audit.register(Project, Database, Credential)
