#    Copyright 2016 Dell Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import eventlet
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
import six

from cinder import exception
from cinder.i18n import _, _LE, _LI, _LW
from cinder.objects import fields
from cinder.volume import driver
from cinder.volume.drivers.dell import dell_storagecenter_api
from cinder.volume.drivers.san.san import san_opts
from cinder.volume import volume_types


common_opts = [
    cfg.IntOpt('dell_sc_ssn',
               default=64702,
               help='Storage Center System Serial Number'),
    cfg.PortOpt('dell_sc_api_port',
                default=3033,
                help='Dell API port'),
    cfg.StrOpt('dell_sc_server_folder',
               default='openstack',
               help='Name of the server folder to use on the Storage Center'),
    cfg.StrOpt('dell_sc_volume_folder',
               default='openstack',
               help='Name of the volume folder to use on the Storage Center'),
    cfg.BoolOpt('dell_sc_verify_cert',
                default=False,
                help='Enable HTTPS SC certificate verification'),
    cfg.StrOpt('secondary_san_ip',
               default='',
               help='IP address of secondary DSM controller'),
    cfg.StrOpt('secondary_san_login',
               default='Admin',
               help='Secondary DSM user name'),
    cfg.StrOpt('secondary_san_password',
               default='',
               help='Secondary DSM user password name',
               secret=True),
    cfg.PortOpt('secondary_sc_api_port',
                default=3033,
                help='Secondary Dell API port')
]

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.register_opts(common_opts)


class DellCommonDriver(driver.ConsistencyGroupVD, driver.ManageableVD,
                       driver.ExtendVD, driver.ManageableSnapshotsVD,
                       driver.SnapshotVD, driver.BaseVD):

    def __init__(self, *args, **kwargs):
        super(DellCommonDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(common_opts)
        self.configuration.append_config_values(san_opts)
        self.backend_name =\
            self.configuration.safe_get('volume_backend_name') or 'Dell'
        self.backends = self.configuration.safe_get('replication_device')
        self.replication_enabled = True if self.backends else False
        self.is_direct_connect = False
        self.active_backend_id = kwargs.get('active_backend_id', None)
        self.failed_over = (self.active_backend_id is not None)
        self.storage_protocol = 'iSCSI'
        self.failback_timeout = 30

    def _bytes_to_gb(self, spacestring):
        """Space is returned in a string like ...

        7.38197504E8 Bytes
        Need to split that apart and convert to GB.

        :returns: gbs in int form
        """
        try:
            n = spacestring.split(' ', 1)
            fgbs = float(n[0]) / 1073741824.0
            igbs = int(fgbs)
            return igbs
        except Exception:
            # If any of that blew up it isn't in the format we
            # thought so eat our error and return None
            return None

    def do_setup(self, context):
        """One time driver setup.

        Called once by the manager after the driver is loaded.
        Sets up clients, check licenses, sets up protocol
        specific helpers.
        """
        self._client = dell_storagecenter_api.StorageCenterApiHelper(
            self.configuration, self.active_backend_id, self.storage_protocol)

    def check_for_setup_error(self):
        """Validates the configuration information."""
        with self._client.open_connection() as api:
            api.find_sc()
            self.is_direct_connect = api.is_direct_connect
            if self.is_direct_connect and self.replication_enabled:
                msg = _('Dell Cinder driver configuration error replication '
                        'not supported with direct connect.')
                raise exception.InvalidHost(reason=msg)

            # If we are a healthy replicated system make sure our backend
            # is alive.
            if self.replication_enabled and not self.failed_over:
                # Check that our replication destinations are available.
                for backend in self.backends:
                    replssn = backend['target_device_id']
                    try:
                        # Just do a find_sc on it.  If it raises we catch
                        # that and raise with a correct exception.
                        api.find_sc(int(replssn))
                    except exception.VolumeBackendAPIException:
                        msg = _('Dell Cinder driver configuration error '
                                'replication_device %s not found') % replssn
                        raise exception.InvalidHost(reason=msg)

    def _get_volume_extra_specs(self, volume):
        """Gets extra specs for the given volume."""
        type_id = volume.get('volume_type_id')
        if type_id:
            return volume_types.get_volume_type_extra_specs(type_id)

        return {}

    def _add_volume_to_consistency_group(self, api, scvolume, volume):
        """Just a helper to add a volume to a consistency group.

        :param api: Dell SC API opbject.
        :param scvolume: Dell SC Volume object.
        :param volume: Cinder Volume object.
        :returns: Nothing.
        """
        if scvolume and volume.get('consistencygroup_id'):
            profile = api.find_replay_profile(
                volume.get('consistencygroup_id'))
            if profile:
                api.update_cg_volumes(profile, [volume])

    def _do_repl(self, api, volume):
        """Checks if we can do replication.

        Need the extra spec set and we have to be talking to EM.

        :param api: Dell REST API object.
        :param volume: Cinder Volume object.
        :return: Boolean (True if replication enabled), Boolean (True if
                 replication type is sync.
        """
        do_repl = False
        sync = False
        # Repl does not work with direct connect.
        if not self.failed_over and not self.is_direct_connect:
            specs = self._get_volume_extra_specs(volume)
            do_repl = specs.get('replication_enabled') == '<is> True'
            sync = specs.get('replication_type') == '<in> sync'
        return do_repl, sync

    def _create_replications(self, api, volume, scvolume):
        """Creates any appropriate replications for a given volume.

        :param api: Dell REST API object.
        :param volume: Cinder volume object.
        :param scvolume: Dell Storage Center Volume object.
        :return: model_update
        """
        # Replication V2
        # for now we assume we have an array named backends.
        replication_driver_data = None
        # Replicate if we are supposed to.
        do_repl, sync = self._do_repl(api, volume)
        if do_repl:
            for backend in self.backends:
                # Check if we are to replicate the active replay or not.
                specs = self._get_volume_extra_specs(volume)
                replact = specs.get('replication:activereplay') == '<is> True'
                if not api.create_replication(scvolume,
                                              backend['target_device_id'],
                                              backend.get('qosnode',
                                                          'cinderqos'),
                                              sync,
                                              backend.get('diskfolder', None),
                                              replact):
                    # Create replication will have printed a better error.
                    msg = _('Replication %(name)s to %(ssn)s failed.') % {
                        'name': volume['id'],
                        'ssn': backend['target_device_id']}
                    raise exception.VolumeBackendAPIException(data=msg)
                if not replication_driver_data:
                    replication_driver_data = backend['target_device_id']
                else:
                    replication_driver_data += ','
                    replication_driver_data += backend['target_device_id']
        # If we did something return model update.
        model_update = {}
        if replication_driver_data:
            model_update = {'replication_status': 'enabled',
                            'replication_driver_data': replication_driver_data}
        return model_update

    @staticmethod
    def _cleanup_failed_create_volume(api, volumename):
        try:
            api.delete_volume(volumename)
        except exception.VolumeBackendAPIException as ex:
            LOG.info(_LI('Non fatal cleanup error: %s.'), ex.msg)

    def create_volume(self, volume):
        """Create a volume."""
        model_update = {}

        # We use id as our name as it is unique.
        volume_name = volume.get('id')
        # Look for our volume
        volume_size = volume.get('size')

        # See if we have any extra specs.
        specs = self._get_volume_extra_specs(volume)
        storage_profile = specs.get('storagetype:storageprofile')
        replay_profile_string = specs.get('storagetype:replayprofiles')

        LOG.debug('Creating volume %(name)s of size %(size)s',
                  {'name': volume_name,
                   'size': volume_size})
        scvolume = None
        with self._client.open_connection() as api:
            try:
                scvolume = api.create_volume(volume_name,
                                             volume_size,
                                             storage_profile,
                                             replay_profile_string)
                if scvolume is None:
                    raise exception.VolumeBackendAPIException(
                        message=_('Unable to create volume %s') %
                        volume_name)

                # Update Consistency Group
                self._add_volume_to_consistency_group(api, scvolume, volume)

                # Create replications. (Or not. It checks.)
                model_update = self._create_replications(api, volume, scvolume)

                # Save our provider_id.
                model_update['provider_id'] = scvolume['instanceId']

            except Exception:
                # if we actually created a volume but failed elsewhere
                # clean up the volume now.
                self._cleanup_failed_create_volume(api, volume_name)
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE('Failed to create volume %s'),
                              volume_name)
        if scvolume is None:
            raise exception.VolumeBackendAPIException(
                data=_('Unable to create volume. Backend down.'))

        return model_update

    def _split_driver_data(self, replication_driver_data):
        """Splits the replication_driver_data into an array of ssn strings.

        :param replication_driver_data: A string of comma separated SSNs.
        :returns: SSNs in an array of strings.
        """
        ssnstrings = []
        # We have any replication_driver_data.
        if replication_driver_data:
            # Split the array and wiffle through the entries.
            for str in replication_driver_data.split(','):
                # Strip any junk from the string.
                ssnstring = str.strip()
                # Anything left?
                if ssnstring:
                    # Add it to our array.
                    ssnstrings.append(ssnstring)
        return ssnstrings

    def _delete_replications(self, api, volume):
        """Delete replications associated with a given volume.

        We should be able to roll through the replication_driver_data list
        of SSNs and delete replication objects between them and the source
        volume.

        :param api: Dell REST API object.
        :param volume: Cinder Volume object
        :return:
        """
        do_repl, sync = self._do_repl(api, volume)
        if do_repl:
            replication_driver_data = volume.get('replication_driver_data')
            if replication_driver_data:
                ssnstrings = self._split_driver_data(replication_driver_data)
                volume_name = volume.get('id')
                provider_id = volume.get('provider_id')
                scvol = api.find_volume(volume_name, provider_id)
                # This is just a string of ssns separated by commas.
                # Trundle through these and delete them all.
                for ssnstring in ssnstrings:
                    ssn = int(ssnstring)
                    if not api.delete_replication(scvol, ssn):
                        LOG.warning(_LW('Unable to delete replication of '
                                        'Volume %(vname)s to Storage Center '
                                        '%(sc)s.'),
                                    {'vname': volume_name,
                                     'sc': ssnstring})
        # If none of that worked or there was nothing to do doesn't matter.
        # Just move on.

    def delete_volume(self, volume):
        deleted = False
        # We use id as our name as it is unique.
        volume_name = volume.get('id')
        provider_id = volume.get('provider_id')
        LOG.debug('Deleting volume %s', volume_name)
        with self._client.open_connection() as api:
            try:
                self._delete_replications(api, volume)
                deleted = api.delete_volume(volume_name, provider_id)
            except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE('Failed to delete volume %s'),
                              volume_name)

        # if there was an error we will have raised an
        # exception.  If it failed to delete it is because
        # the conditions to delete a volume were not met.
        if deleted is False:
            raise exception.VolumeIsBusy(volume_name=volume_name)

    def create_snapshot(self, snapshot):
        """Create snapshot"""
        # our volume name is the volume id
        volume_name = snapshot.get('volume_id')
        # TODO(tswanson): Is there any reason to think this will be set
        # before I create the snapshot? Doesn't hurt to try to get it.
        provider_id = snapshot.get('provider_id')
        snapshot_id = snapshot.get('id')
        LOG.debug('Creating snapshot %(snap)s on volume %(vol)s',
                  {'snap': snapshot_id,
                   'vol': volume_name})
        with self._client.open_connection() as api:
            scvolume = api.find_volume(volume_name, provider_id)
            if scvolume is not None:
                replay = api.create_replay(scvolume, snapshot_id, 0)
                if replay:
                    return {'status': 'available',
                            'provider_id': scvolume['instanceId']}
            else:
                LOG.warning(_LW('Unable to locate volume:%s'),
                            volume_name)

        snapshot['status'] = 'error_creating'
        msg = _('Failed to create snapshot %s') % snapshot_id
        raise exception.VolumeBackendAPIException(data=msg)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Create new volume from other volume's snapshot on appliance."""
        model_update = {}
        scvolume = None
        volume_name = volume.get('id')
        src_provider_id = snapshot.get('provider_id')
        src_volume_name = snapshot.get('volume_id')
        # This snapshot could have been created on its own or as part of a
        # cgsnapshot.  If it was a cgsnapshot it will be identified on the Dell
        # backend under cgsnapshot_id.  Given the volume ID and the
        # cgsnapshot_id we can find the appropriate snapshot.
        # So first we look for cgsnapshot_id.  If that is blank then it must
        # have been a normal snapshot which will be found under snapshot_id.
        snapshot_id = snapshot.get('cgsnapshot_id')
        if not snapshot_id:
            snapshot_id = snapshot.get('id')
        LOG.debug(
            'Creating new volume %(vol)s from snapshot %(snap)s '
            'from vol %(src)s',
            {'vol': volume_name,
             'snap': snapshot_id,
             'src': src_volume_name})
        with self._client.open_connection() as api:
            try:
                srcvol = api.find_volume(src_volume_name, src_provider_id)
                if srcvol is not None:
                    replay = api.find_replay(srcvol, snapshot_id)
                    if replay is not None:
                        # See if we have any extra specs.
                        specs = self._get_volume_extra_specs(volume)
                        replay_profile_string = specs.get(
                            'storagetype:replayprofiles')
                        scvolume = api.create_view_volume(
                            volume_name, replay, replay_profile_string)

                        # Extend Volume
                        if scvolume and (volume['size'] >
                                         snapshot["volume_size"]):
                            LOG.debug('Resize the new volume to %s.',
                                      volume['size'])
                            scvolume = api.expand_volume(scvolume,
                                                         volume['size'])
                        if scvolume is None:
                            raise exception.VolumeBackendAPIException(
                                message=_('Unable to create volume '
                                          '%(name)s from %(snap)s.') %
                                {'name': volume_name,
                                 'snap': snapshot_id})

                        # Update Consistency Group
                        self._add_volume_to_consistency_group(api,
                                                              scvolume,
                                                              volume)
                        # Replicate if we are supposed to.
                        model_update = self._create_replications(api,
                                                                 volume,
                                                                 scvolume)
                        # Save our instanceid.
                        model_update['provider_id'] = (
                            scvolume['instanceId'])

            except Exception:
                # Clean up after ourselves.
                self._cleanup_failed_create_volume(api, volume_name)
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE('Failed to create volume %s'),
                              volume_name)
        if scvolume is not None:
            LOG.debug('Volume %(vol)s created from %(snap)s',
                      {'vol': volume_name,
                       'snap': snapshot_id})
        else:
            msg = _('Failed to create volume %s') % volume_name
            raise exception.VolumeBackendAPIException(data=msg)

        return model_update

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume."""
        model_update = {}
        scvolume = None
        src_volume_name = src_vref.get('id')
        src_provider_id = src_vref.get('provider_id')
        volume_name = volume.get('id')
        LOG.debug('Creating cloned volume %(clone)s from volume %(vol)s',
                  {'clone': volume_name,
                   'vol': src_volume_name})
        with self._client.open_connection() as api:
            try:
                srcvol = api.find_volume(src_volume_name, src_provider_id)
                if srcvol is not None:
                    # See if we have any extra specs.
                    specs = self._get_volume_extra_specs(volume)
                    replay_profile_string = specs.get(
                        'storagetype:replayprofiles')
                    # Create our volume
                    scvolume = api.create_cloned_volume(
                        volume_name, srcvol, replay_profile_string)

                    # Extend Volume
                    if scvolume and volume['size'] > src_vref['size']:
                        LOG.debug('Resize the volume to %s.', volume['size'])
                        scvolume = api.expand_volume(scvolume, volume['size'])

                    # If either of those didn't work we bail.
                    if scvolume is None:
                        raise exception.VolumeBackendAPIException(
                            message=_('Unable to create volume '
                                      '%(name)s from %(vol)s.') %
                            {'name': volume_name,
                             'vol': src_volume_name})

                    # Update Consistency Group
                    self._add_volume_to_consistency_group(api,
                                                          scvolume,
                                                          volume)
                    # Replicate if we are supposed to.
                    model_update = self._create_replications(api,
                                                             volume,
                                                             scvolume)
                    # Save our provider_id.
                    model_update['provider_id'] = scvolume['instanceId']
            except Exception:
                # Clean up after ourselves.
                self._cleanup_failed_create_volume(api, volume_name)
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE('Failed to create volume %s'),
                              volume_name)
        if scvolume is not None:
            LOG.debug('Volume %(vol)s cloned from %(src)s',
                      {'vol': volume_name,
                       'src': src_volume_name})
        else:
            msg = _('Failed to create volume %s') % volume_name
            raise exception.VolumeBackendAPIException(data=msg)
        return model_update

    def delete_snapshot(self, snapshot):
        """delete_snapshot"""
        volume_name = snapshot.get('volume_id')
        snapshot_id = snapshot.get('id')
        provider_id = snapshot.get('provider_id')
        LOG.debug('Deleting snapshot %(snap)s from volume %(vol)s',
                  {'snap': snapshot_id,
                   'vol': volume_name})
        with self._client.open_connection() as api:
            scvolume = api.find_volume(volume_name, provider_id)
            if scvolume and api.delete_replay(scvolume, snapshot_id):
                return
        # if we are here things went poorly.
        snapshot['status'] = 'error_deleting'
        msg = _('Failed to delete snapshot %s') % snapshot_id
        raise exception.VolumeBackendAPIException(data=msg)

    def create_export(self, context, volume, connector):
        """Create an export of a volume.

        The volume exists on creation and will be visible on
        initialize connection.  So nothing to do here.
        """
        pass

    def ensure_export(self, context, volume):
        """Ensure an export of a volume.

        Per the eqlx driver we just make sure that the volume actually
        exists where we think it does.
        """
        scvolume = None
        volume_name = volume.get('id')
        provider_id = volume.get('provider_id')
        LOG.debug('Checking existence of volume %s', volume_name)
        with self._client.open_connection() as api:
            try:
                scvolume = api.find_volume(volume_name, provider_id)
            except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.error(_LE('Failed to ensure export of volume %s'),
                              volume_name)
        if scvolume is None:
            msg = _('Unable to find volume %s') % volume_name
            raise exception.VolumeBackendAPIException(data=msg)

    def remove_export(self, context, volume):
        """Remove an export of a volume.

        We do nothing here to match the nothing we do in create export.  Again
        we do everything in initialize and terminate connection.
        """
        pass

    def extend_volume(self, volume, new_size):
        """Extend the size of the volume."""
        volume_name = volume.get('id')
        provider_id = volume.get('provider_id')
        LOG.debug('Extending volume %(vol)s to %(size)s',
                  {'vol': volume_name,
                   'size': new_size})
        if volume is not None:
            with self._client.open_connection() as api:
                scvolume = api.find_volume(volume_name, provider_id)
                if api.expand_volume(scvolume, new_size) is not None:
                    return
        # If we are here nothing good happened.
        msg = _('Unable to extend volume %s') % volume_name
        raise exception.VolumeBackendAPIException(data=msg)

    def get_volume_stats(self, refresh=False):
        """Get volume status.

        If 'refresh' is True, run update the stats first.
        """
        if refresh:
            self._update_volume_stats()

        return self._stats

    def _update_volume_stats(self):
        """Retrieve stats info from volume group."""
        with self._client.open_connection() as api:
            storageusage = api.get_storage_usage()
            if not storageusage:
                msg = _('Unable to retrieve volume stats.')
                raise exception.VolumeBackendAPIException(message=msg)

            # all of this is basically static for now
            data = {}
            data['volume_backend_name'] = self.backend_name
            data['vendor_name'] = 'Dell'
            data['driver_version'] = self.VERSION
            data['storage_protocol'] = self.storage_protocol
            data['reserved_percentage'] = 0
            data['consistencygroup_support'] = True
            data['thin_provisioning_support'] = True
            totalcapacity = storageusage.get('availableSpace')
            totalcapacitygb = self._bytes_to_gb(totalcapacity)
            data['total_capacity_gb'] = totalcapacitygb
            freespace = storageusage.get('freeSpace')
            freespacegb = self._bytes_to_gb(freespace)
            data['free_capacity_gb'] = freespacegb
            data['QoS_support'] = False
            data['replication_enabled'] = self.replication_enabled
            if self.replication_enabled:
                data['replication_type'] = ['async', 'sync']
                data['replication_count'] = len(self.backends)
                replication_targets = []
                # Trundle through our backends.
                for backend in self.backends:
                    target_device_id = backend.get('target_device_id')
                    if target_device_id:
                        replication_targets.append(target_device_id)
                data['replication_targets'] = replication_targets

            self._stats = data
            LOG.debug('Total cap %(total)s Free cap %(free)s',
                      {'total': data['total_capacity_gb'],
                       'free': data['free_capacity_gb']})

    def update_migrated_volume(self, ctxt, volume, new_volume,
                               original_volume_status):
        """Return model update for migrated volume.

        :param volume: The original volume that was migrated to this backend
        :param new_volume: The migration volume object that was created on
                           this backend as part of the migration process
        :param original_volume_status: The status of the original volume
        :returns: model_update to update DB with any needed changes
        """
        # We use id as our volume name so we need to rename the backend
        # volume to the original volume name.
        original_volume_name = volume.get('id')
        current_name = new_volume.get('id')
        # We should have this. If we don't we'll set it below.
        provider_id = new_volume.get('provider_id')
        LOG.debug('update_migrated_volume: %(current)s to %(original)s',
                  {'current': current_name,
                   'original': original_volume_name})
        if original_volume_name:
            with self._client.open_connection() as api:
                scvolume = api.find_volume(current_name, provider_id)
                if (scvolume and
                   api.rename_volume(scvolume, original_volume_name)):
                    # Replicate if we are supposed to.
                    model_update = self._create_replications(api,
                                                             new_volume,
                                                             scvolume)
                    model_update['_name_id'] = None
                    model_update['provider_id'] = scvolume['instanceId']

                    return model_update
        # The world was horrible to us so we should error and leave.
        LOG.error(_LE('Unable to rename the logical volume for volume: %s'),
                  original_volume_name)

        return {'_name_id': new_volume['_name_id'] or new_volume['id']}

    def create_consistencygroup(self, context, group):
        """This creates a replay profile on the storage backend.

        :param context: the context of the caller.
        :param group: the dictionary of the consistency group to be created.
        :returns: Nothing on success.
        :raises: VolumeBackendAPIException
        """
        gid = group['id']
        with self._client.open_connection() as api:
            cgroup = api.create_replay_profile(gid)
            if cgroup:
                LOG.info(_LI('Created Consistency Group %s'), gid)
                return
        msg = _('Unable to create consistency group %s') % gid
        raise exception.VolumeBackendAPIException(data=msg)

    def delete_consistencygroup(self, context, group, volumes):
        """Delete the Dell SC profile associated with this consistency group.

        :param context: the context of the caller.
        :param group: the dictionary of the consistency group to be created.
        :returns: Updated model_update, volumes.
        """
        gid = group['id']
        with self._client.open_connection() as api:
            profile = api.find_replay_profile(gid)
            if profile:
                api.delete_replay_profile(profile)
        # If we are here because we found no profile that should be fine
        # as we are trying to delete it anyway.

        # Trundle through the list deleting the volumes.
        for volume in volumes:
            self.delete_volume(volume)
            volume['status'] = 'deleted'

        model_update = {'status': group['status']}

        return model_update, volumes

    def update_consistencygroup(self, context, group,
                                add_volumes=None, remove_volumes=None):
        """Updates a consistency group.

        :param context: the context of the caller.
        :param group: the dictionary of the consistency group to be updated.
        :param add_volumes: a list of volume dictionaries to be added.
        :param remove_volumes: a list of volume dictionaries to be removed.
        :returns: model_update, add_volumes_update, remove_volumes_update

        model_update is a dictionary that the driver wants the manager
        to update upon a successful return. If None is returned, the manager
        will set the status to 'available'.

        add_volumes_update and remove_volumes_update are lists of dictionaries
        that the driver wants the manager to update upon a successful return.
        Note that each entry requires a {'id': xxx} so that the correct
        volume entry can be updated. If None is returned, the volume will
        remain its original status. Also note that you cannot directly
        assign add_volumes to add_volumes_update as add_volumes is a list of
        cinder.db.sqlalchemy.models.Volume objects and cannot be used for
        db update directly. Same with remove_volumes.

        If the driver throws an exception, the status of the group as well as
        those of the volumes to be added/removed will be set to 'error'.
        """
        gid = group['id']
        with self._client.open_connection() as api:
            profile = api.find_replay_profile(gid)
            if not profile:
                LOG.error(_LE('Cannot find Consistency Group %s'), gid)
            elif api.update_cg_volumes(profile,
                                       add_volumes,
                                       remove_volumes):
                LOG.info(_LI('Updated Consistency Group %s'), gid)
                # we need nothing updated above us so just return None.
                return None, None, None
        # Things did not go well so throw.
        msg = _('Unable to update consistency group %s') % gid
        raise exception.VolumeBackendAPIException(data=msg)

    def create_cgsnapshot(self, context, cgsnapshot, snapshots):
        """Takes a snapshot of the consistency group.

        :param context: the context of the caller.
        :param cgsnapshot: Information about the snapshot to take.
        :param snapshots: List of snapshots for this cgsnapshot.
        :returns: Updated model_update, snapshots.
        :raises: VolumeBackendAPIException.
        """
        cgid = cgsnapshot['consistencygroup_id']
        snapshotid = cgsnapshot['id']

        with self._client.open_connection() as api:
            profile = api.find_replay_profile(cgid)
            if profile:
                LOG.debug('profile %s replayid %s', profile, snapshotid)
                if api.snap_cg_replay(profile, snapshotid, 0):
                    snapshot_updates = []
                    for snapshot in snapshots:
                        snapshot_updates.append({
                            'id': snapshot.id,
                            'status': fields.SnapshotStatus.AVAILABLE
                        })

                    model_update = {'status': 'available'}

                    return model_update, snapshot_updates

                # That didn't go well.  Tell them why.  Then bomb out.
                LOG.error(_LE('Failed to snap Consistency Group %s'), cgid)
            else:
                LOG.error(_LE('Cannot find Consistency Group %s'), cgid)

        msg = _('Unable to snap Consistency Group %s') % cgid
        raise exception.VolumeBackendAPIException(data=msg)

    def delete_cgsnapshot(self, context, cgsnapshot, snapshots):
        """Deletes a cgsnapshot.

        If profile isn't found return success.  If failed to delete the
        replay (the snapshot) then raise an exception.

        :param context: the context of the caller.
        :param cgsnapshot: Information about the snapshot to delete.
        :returns: Updated model_update, snapshots.
        :raises: VolumeBackendAPIException.
        """
        cgid = cgsnapshot['consistencygroup_id']
        snapshotid = cgsnapshot['id']

        with self._client.open_connection() as api:
            profile = api.find_replay_profile(cgid)
            if profile:
                LOG.info(_LI('Deleting snapshot %(ss)s from %(pro)s'),
                         {'ss': snapshotid,
                          'pro': profile})
                if not api.delete_cg_replay(profile, snapshotid):
                    msg = (_('Unable to delete Consistency Group snapshot %s')
                           % snapshotid)
                    raise exception.VolumeBackendAPIException(data=msg)

            for snapshot in snapshots:
                snapshot.status = fields.SnapshotStatus.DELETED

            model_update = {'status': 'deleted'}

            return model_update, snapshots

    def manage_existing(self, volume, existing_ref):
        """Brings an existing backend storage object under Cinder management.

        existing_ref is passed straight through from the API request's
        manage_existing_ref value, and it is up to the driver how this should
        be interpreted.  It should be sufficient to identify a storage object
        that the driver should somehow associate with the newly-created cinder
        volume structure.

        There are two ways to do this:

        1. Rename the backend storage object so that it matches the,
           volume['name'] which is how drivers traditionally map between a
           cinder volume and the associated backend storage object.

        2. Place some metadata on the volume, or somewhere in the backend, that
           allows other driver requests (e.g. delete, clone, attach, detach...)
           to locate the backend storage object when required.

        If the existing_ref doesn't make sense, or doesn't refer to an existing
        backend storage object, raise a ManageExistingInvalidReference
        exception.

        The volume may have a volume_type, and the driver can inspect that and
        compare against the properties of the referenced backend storage
        object.  If they are incompatible, raise a
        ManageExistingVolumeTypeMismatch, specifying a reason for the failure.

        :param volume:       Cinder volume to manage
        :param existing_ref: Driver-specific information used to identify a
                             volume
        """
        if existing_ref.get('source-name') or existing_ref.get('source-id'):
            with self._client.open_connection() as api:
                api.manage_existing(volume['id'], existing_ref)
                # Replicate if we are supposed to.
                volume_name = volume.get('id')
                provider_id = volume.get('provider_id')
                scvolume = api.find_volume(volume_name, provider_id)
                model_update = self._create_replications(api, volume, scvolume)
                if model_update:
                    return model_update
        else:
            msg = _('Must specify source-name or source-id.')
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref, reason=msg)
        # Only return a model_update if we have replication info to add.
        return None

    def manage_existing_get_size(self, volume, existing_ref):
        """Return size of volume to be managed by manage_existing.

        When calculating the size, round up to the next GB.

        :param volume:       Cinder volume to manage
        :param existing_ref: Driver-specific information used to identify a
                             volume
        """
        if existing_ref.get('source-name') or existing_ref.get('source-id'):
            with self._client.open_connection() as api:
                return api.get_unmanaged_volume_size(existing_ref)
        else:
            msg = _('Must specify source-name or source-id.')
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref, reason=msg)

    def unmanage(self, volume):
        """Removes the specified volume from Cinder management.

        Does not delete the underlying backend storage object.

        For most drivers, this will not need to do anything.  However, some
        drivers might use this call as an opportunity to clean up any
        Cinder-specific configuration that they have associated with the
        backend storage object.

        :param volume: Cinder volume to unmanage
        """
        with self._client.open_connection() as api:
            volume_name = volume.get('id')
            provider_id = volume.get('provider_id')
            scvolume = api.find_volume(volume_name, provider_id)
            if scvolume:
                api.unmanage(scvolume)

    def _get_retype_spec(self, diff, volume_name, specname, spectype):
        """Helper function to get current and requested spec.

        :param diff: A difference dictionary.
        :param volume_name: The volume name we are working with.
        :param specname: The pretty name of the parameter.
        :param spectype: The actual spec string.
        :return: current, requested spec.
        :raises: VolumeBackendAPIException
        """
        spec = (diff['extra_specs'].get(spectype))
        if spec:
            if len(spec) != 2:
                msg = _('Unable to retype %(specname)s, expected to receive '
                        'current and requested %(spectype)s values. Value '
                        'received: %(spec)s') % {'specname': specname,
                                                 'spectype': spectype,
                                                 'spec': spec}
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)

            current = spec[0]
            requested = spec[1]

            if current != requested:
                LOG.debug('Retyping volume %(vol)s to use %(specname)s '
                          '%(spec)s.',
                          {'vol': volume_name,
                           'specname': specname,
                           'spec': requested})
                return current, requested
            else:
                LOG.info(_LI('Retype was to same Storage Profile.'))
        return None, None

    def retype(self, ctxt, volume, new_type, diff, host):
        """Convert the volume to be of the new type.

        Returns a boolean indicating whether the retype occurred.

        :param ctxt: Context
        :param volume: A dictionary describing the volume to migrate
        :param new_type: A dictionary describing the volume type to convert to
        :param diff: A dictionary with the difference between the two types
        :param host: A dictionary describing the host to migrate to, where
                     host['host'] is its name, and host['capabilities'] is a
                     dictionary of its reported capabilities (Not Used).
        """
        LOG.info(_LI('retype: volume_name: %(name)s new_type: %(newtype)s '
                     'diff: %(diff)s host: %(host)s'),
                 {'name': volume.get('id'), 'newtype': new_type,
                  'diff': diff, 'host': host})
        model_update = None
        # Any spec changes?
        if diff['extra_specs']:
            volume_name = volume.get('id')
            provider_id = volume.get('provider_id')
            with self._client.open_connection() as api:
                try:
                    # Get our volume
                    scvolume = api.find_volume(volume_name, provider_id)
                    if scvolume is None:
                        LOG.error(_LE('Retype unable to find volume %s.'),
                                  volume_name)
                        return False
                    # Check our specs.
                    # Storage profiles.
                    current, requested = (
                        self._get_retype_spec(diff, volume_name,
                                              'Storage Profile',
                                              'storagetype:storageprofile'))
                    # if there is a change and it didn't work fast fail.
                    if (current != requested and not
                       api.update_storage_profile(scvolume, requested)):
                        LOG.error(_LE('Failed to update storage profile'))
                        return False

                    # Replay profiles.
                    current, requested = (
                        self._get_retype_spec(diff, volume_name,
                                              'Replay Profiles',
                                              'storagetype:replayprofiles'))
                    # if there is a change and it didn't work fast fail.
                    if requested and not api.update_replay_profiles(scvolume,
                                                                    requested):
                        LOG.error(_LE('Failed to update replay profiles'))
                        return False

                    # Replication_enabled.
                    current, requested = (
                        self._get_retype_spec(diff,
                                              volume_name,
                                              'replication_enabled',
                                              'replication_enabled'))
                    # if there is a change and it didn't work fast fail.
                    if current != requested:
                        if requested == '<is> True':
                            model_update = self._create_replications(api,
                                                                     volume,
                                                                     scvolume)
                        elif current == '<is> True':
                            self._delete_replications(api, volume)
                            model_update = {'replication_status': 'disabled',
                                            'replication_driver_data': ''}

                    # Active Replay
                    current, requested = (
                        self._get_retype_spec(diff, volume_name,
                                              'Replicate Active Replay',
                                              'replication:activereplay'))
                    if current != requested and not (
                            api.update_replicate_active_replay(
                                scvolume, requested == '<is> True')):
                        LOG.error(_LE('Failed to apply '
                                      'replication:activereplay setting'))
                        return False

                    # TODO(tswanson): replaytype once it actually works.

                except exception.VolumeBackendAPIException:
                    # We do nothing with this. We simply return failure.
                    return False
        # If we have something to send down...
        if model_update:
            return model_update
        return True

    def _parse_secondary(self, api, secondary):
        """Find the replication destination associated with secondary.

        :param api: Dell StorageCenterApi
        :param secondary: String indicating the secondary to failover to.
        :return: Destination SSN for the given secondary.
        """
        LOG.debug('_parse_secondary. Looking for %s.', secondary)
        destssn = None
        # Trundle through these looking for our secondary.
        for backend in self.backends:
            ssnstring = backend['target_device_id']
            # If they list a secondary it has to match.
            # If they do not list a secondary we return the first
            # replication on a working system.
            if not secondary or secondary == ssnstring:
                # Is a string.  Need an int.
                ssn = int(ssnstring)
                # Without the source being up we have no good
                # way to pick a destination to failover to. So just
                # look for one that is just up.
                try:
                    # If the SC ssn exists use it.
                    if api.find_sc(ssn):
                        destssn = ssn
                        break
                except exception.VolumeBackendAPIException:
                    LOG.warning(_LW('SSN %s appears to be down.'), ssn)
        LOG.info(_LI('replication failover secondary is %(ssn)s'),
                 {'ssn': destssn})
        return destssn

    def _update_backend(self, active_backend_id):
        # Mark for failover or undo failover.
        LOG.debug('active_backend_id: %s', active_backend_id)
        if active_backend_id:
            self.active_backend_id = six.text_type(active_backend_id)
            self.failed_over = True
        else:
            self.active_backend_id = None
            self.failed_over = False

        self._client.active_backend_id = self.active_backend_id

    def _get_qos(self, targetssn):
        # Find our QOS.
        qosnode = None
        for backend in self.backends:
            if int(backend['target_device_id']) == targetssn:
                qosnode = backend.get('qosnode', 'cinderqos')
        return qosnode

    def _parse_extraspecs(self, volume):
        # Digest our extra specs.
        extraspecs = {}
        specs = self._get_volume_extra_specs(volume)
        if specs.get('replication_type') == '<in> sync':
            extraspecs['replicationtype'] = 'Synchronous'
        else:
            extraspecs['replicationtype'] = 'Asynchronous'
        if specs.get('replication:activereplay') == '<is> True':
            extraspecs['activereplay'] = True
        else:
            extraspecs['activereplay'] = False
        extraspecs['storage_profile'] = specs.get('storagetype:storageprofile')
        extraspecs['replay_profile_string'] = (
            specs.get('storagetype:replayprofiles'))
        return extraspecs

    def _wait_for_replication(self, api, items):
        # Wait for our replications to resync with their original volumes.
        # We wait for completion, errors or timeout.
        deadcount = 5
        lastremain = 0.0
        # The big wait loop.
        while True:
            # We run until all volumes are synced or in error.
            done = True
            currentremain = 0.0
            # Run the list.
            for item in items:
                # If we have one cooking.
                if item['status'] == 'inprogress':
                    # Is it done?
                    synced, remain = api.replication_progress(item['screpl'])
                    currentremain += remain
                    if synced:
                        # It is! Get our volumes.
                        cvol = api.get_volume(item['cvol'])
                        nvol = api.get_volume(item['nvol'])

                        # Flip replication.
                        if (cvol and nvol and api.flip_replication(
                                cvol, nvol, item['volume']['id'],
                                item['specs']['replicationtype'],
                                item['qosnode'],
                                item['specs']['activereplay'])):
                            # rename the original. Doesn't matter if it
                            # succeeded as we should have the provider_id
                            # of the new volume.
                            ovol = api.get_volume(item['ovol'])
                            if not ovol or not api.rename_volume(
                                    ovol, 'org:' + ovol['name']):
                                # Not a reason to fail but will possibly
                                # cause confusion so warn.
                                LOG.warning(_LW('Unable to locate and rename '
                                                'original volume: %s'),
                                            item['ovol'])
                            item['status'] = 'synced'
                        else:
                            item['status'] = 'error'
                    elif synced is None:
                        # Couldn't get info on this one. Call it baked.
                        item['status'] = 'error'
                    else:
                        # Miles to go before we're done.
                        done = False
            # done? then leave.
            if done:
                break

            # Confirm we are or are not still making progress.
            if lastremain == currentremain:
                # One chance down. Warn user.
                deadcount -= 1
                LOG.warning(_LW('Waiting for replications to complete. '
                                'No progress for 30 seconds. deadcount = %d'),
                            deadcount)
            else:
                # Reset
                lastremain = currentremain
                deadcount = 5

            # If we've used up our 5 chances we error and log..
            if deadcount == 0:
                LOG.error(_LE('Replication progress has stopped.'))
                for item in items:
                    if item['status'] == 'inprogress':
                        LOG.error(_LE('Failback failed for volume: %s. '
                                      'Timeout waiting for replication to '
                                      'sync with original volume.'),
                                  item['volume']['id'])
                        item['status'] = 'error'
                break
            # This is part of an async call so we should be good sleeping here.
            # Have to balance hammering the backend for no good reason with
            # the max timeout for the unit tests. Yeah, silly.
            eventlet.sleep(self.failback_timeout)

    def _reattach_remaining_replications(self, api, items):
        # Wiffle through our backends and reattach any remaining replication
        # targets.
        for item in items:
            if item['status'] == 'synced':
                svol = api.get_volume(item['nvol'])
                # assume it went well. Will error out if not.
                item['status'] = 'reattached'
                # wiffle through our backends and kick off replications.
                for backend in self.backends:
                    rssn = int(backend['target_device_id'])
                    if rssn != api.ssn:
                        rvol = api.find_repl_volume(item['volume']['id'],
                                                    rssn, None)
                        # if there is an old replication whack it.
                        api.delete_replication(svol, rssn, False)
                        if api.start_replication(
                                svol, rvol,
                                item['specs']['replicationtype'],
                                self._get_qos(rssn),
                                item['specs']['activereplay']):
                            # Save our replication_driver_data.
                            item['rdd'] += ','
                            item['rdd'] += backend['target_device_id']
                        else:
                            # No joy. Bail
                            item['status'] = 'error'

    def _fixup_types(self, api, items):
        # Update our replay profiles.
        for item in items:
            if item['status'] == 'reattached':
                # Re-apply any appropriate replay profiles.
                item['status'] = 'available'
                rps = item['specs']['replay_profile_string']
                if rps:
                    svol = api.get_volume(item['nvol'])
                    if not api.update_replay_profiles(svol, rps):
                        item['status'] = 'error'

    def _volume_updates(self, items):
        # Update our volume updates.
        volume_updates = []
        for item in items:
            # Set our status for our replicated volumes
            model_update = {'provider_id': item['nvol'],
                            'replication_driver_data': item['rdd']}
            # These are simple. If the volume reaches available then,
            # since we were replicating it, replication status must
            # be good. Else error/error.
            if item['status'] == 'available':
                model_update['status'] = 'available'
                model_update['replication_status'] = 'enabled'
            else:
                model_update['status'] = 'error'
                model_update['replication_status'] = 'error'
            volume_updates.append({'volume_id': item['volume']['id'],
                                   'updates': model_update})
        return volume_updates

    def failback_volumes(self, volumes):
        """This is a generic volume failback.

        :param volumes: List of volumes that need to be failed back.
        :return: volume_updates for the list of volumes.
        """
        LOG.info(_LI('failback_volumes'))
        with self._client.open_connection() as api:
            # Get our qosnode. This is a good way to make sure the backend
            # is still setup so that we can do this.
            qosnode = self._get_qos(api.ssn)
            if not qosnode:
                raise exception.VolumeBackendAPIException(
                    message=_('Unable to failback. Backend is misconfigured.'))

            volume_updates = []
            replitems = []
            screplid = None
            status = ''
            # Trundle through the volumes. Update non replicated to alive again
            # and reverse the replications for the remaining volumes.
            for volume in volumes:
                LOG.info(_LI('failback_volumes: starting volume: %s'), volume)
                model_update = {}
                if volume.get('replication_driver_data'):
                    LOG.info(_LI('failback_volumes: replicated volume'))
                    # Get our current volume.
                    cvol = api.find_volume(volume['id'], volume['provider_id'])
                    # Original volume on the primary.
                    ovol = api.find_repl_volume(volume['id'], api.primaryssn,
                                                None, True, False)
                    # Delete our current mappings.
                    api.remove_mappings(cvol)
                    # If there is a replication to delete do so.
                    api.delete_replication(ovol, api.ssn, False)
                    # Replicate to a common replay.
                    screpl = api.replicate_to_common(cvol, ovol, 'tempqos')
                    # We made it this far. Update our status.
                    if screpl:
                        screplid = screpl['instanceId']
                        nvolid = screpl['destinationVolume']['instanceId']
                        status = 'inprogress'
                    else:
                        LOG.error(_LE('Unable to restore %s'), volume['id'])
                        screplid = None
                        nvolid = None
                        status = 'error'

                    # Save some information for the next step.
                    # nvol is the new volume created by replicate_to_common.
                    # We also grab our extra specs here.
                    replitems.append(
                        {'volume': volume,
                         'specs': self._parse_extraspecs(volume),
                         'qosnode': qosnode,
                         'screpl': screplid,
                         'cvol': cvol['instanceId'],
                         'ovol': ovol['instanceId'],
                         'nvol': nvolid,
                         'rdd': six.text_type(api.ssn),
                         'status': status})
                else:
                    # Not replicated. Just set it to available.
                    model_update = {'status': 'available'}
                    # Either we are failed over or our status is now error.
                    volume_updates.append({'volume_id': volume['id'],
                                           'updates': model_update})

            if replitems:
                # Wait for replication to complete.
                # This will also flip replication.
                self._wait_for_replication(api, replitems)
                # Replications are done. Attach to any additional replication
                # backends.
                self._reattach_remaining_replications(api, replitems)
                self._fixup_types(api, replitems)
                volume_updates += self._volume_updates(replitems)

            # Set us back to a happy state.
            # The only way this doesn't happen is if the primary is down.
            self._update_backend(None)
            return volume_updates

    def failover_host(self, context, volumes, secondary_id=None):
        """Failover to secondary.

        :param context: security context
        :param secondary_id: Specifies rep target to fail over to
        :param volumes: List of volumes serviced by this backend.
        :returns: destssn, volume_updates data structure

        Example volume_updates data structure:

        .. code-block:: json

        [{'volume_id': <cinder-uuid>,
          'updates': {'provider_id': 8,
                      'replication_status': 'failed-over',
                      'replication_extended_status': 'whatever',...}},]
        """

        LOG.debug('failover-host')
        LOG.debug(self.failed_over)
        LOG.debug(self.active_backend_id)
        LOG.debug(self.replication_enabled)
        if self.failed_over:
            if secondary_id == 'default':
                LOG.debug('failing back')
                return 'default', self.failback_volumes(volumes)
            raise exception.VolumeBackendAPIException(
                message='Already failed over.')

        LOG.info(_LI('Failing backend to %s'), secondary_id)
        # basic check
        if self.replication_enabled:
            with self._client.open_connection() as api:
                # Look for the specified secondary.
                destssn = self._parse_secondary(api, secondary_id)
                if destssn:
                    # We roll through trying to break replications.
                    # Is failing here a complete failure of failover?
                    volume_updates = []
                    for volume in volumes:
                        model_update = {}
                        if volume.get('replication_driver_data'):
                            rvol = api.break_replication(
                                volume['id'], volume.get('provider_id'),
                                destssn)
                            if rvol:
                                LOG.info(_LI('Success failing over volume %s'),
                                         volume['id'])
                            else:
                                LOG.info(_LI('Failed failing over volume %s'),
                                         volume['id'])

                            # We should note that we are now failed over
                            # and that we have a new instanceId.
                            model_update = {
                                'replication_status': 'failed-over',
                                'provider_id': rvol['instanceId']}
                        else:
                            # Not a replicated volume. Try to unmap it.
                            scvolume = api.find_volume(
                                volume['id'], volume.get('provider_id'))
                            api.remove_mappings(scvolume)
                            model_update = {'status': 'error'}
                        # Either we are failed over or our status is now error.
                        volume_updates.append({'volume_id': volume['id'],
                                               'updates': model_update})

                    # this is it.
                    self._update_backend(destssn)
                    LOG.debug('after update backend')
                    LOG.debug(self.failed_over)
                    LOG.debug(self.active_backend_id)
                    LOG.debug(self.replication_enabled)
                    return destssn, volume_updates
                else:
                    raise exception.InvalidInput(message=(
                        _('replication_failover failed. %s not found.') %
                        secondary_id))
        # I don't think we should ever get here.
        raise exception.VolumeBackendAPIException(message=(
            _('replication_failover failed. '
              'Backend not configured for failover')))

    def _get_unmanaged_replay(self, api, volume_name, provider_id,
                              existing_ref):
        replay_name = None
        if existing_ref:
            replay_name = existing_ref.get('source-name')
        if not replay_name:
            msg = _('_get_unmanaged_replay: Must specify source-name.')
            LOG.error(msg)
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref, reason=msg)
        # Find our volume.
        scvolume = api.find_volume(volume_name, provider_id)
        if not scvolume:
            # Didn't find it.
            msg = (_('_get_unmanaged_replay: Cannot find volume id %s')
                   % volume_name)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        # Find our replay.
        screplay = api.find_replay(scvolume, replay_name)
        if not screplay:
            # Didn't find it. Reference must be invalid.
            msg = (_('_get_unmanaged_replay: Cannot '
                     'find snapshot named %s') % replay_name)
            LOG.error(msg)
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref, reason=msg)
        return screplay

    def manage_existing_snapshot(self, snapshot, existing_ref):
        """Brings an existing backend storage object under Cinder management.

        existing_ref is passed straight through from the API request's
        manage_existing_ref value, and it is up to the driver how this should
        be interpreted.  It should be sufficient to identify a storage object
        that the driver should somehow associate with the newly-created cinder
        snapshot structure.

        There are two ways to do this:

        1. Rename the backend storage object so that it matches the
           snapshot['name'] which is how drivers traditionally map between a
           cinder snapshot and the associated backend storage object.

        2. Place some metadata on the snapshot, or somewhere in the backend,
           that allows other driver requests (e.g. delete) to locate the
           backend storage object when required.

        If the existing_ref doesn't make sense, or doesn't refer to an existing
        backend storage object, raise a ManageExistingInvalidReference
        exception.
        """
        with self._client.open_connection() as api:
            # Find our unmanaged snapshot. This will raise on error.
            volume_name = snapshot.get('volume_id')
            provider_id = snapshot.get('provider_id')
            snapshot_id = snapshot.get('id')
            screplay = self._get_unmanaged_replay(api, volume_name,
                                                  provider_id, existing_ref)
            # Manage means update description and update expiration.
            if not api.manage_replay(screplay, snapshot_id):
                # That didn't work. Error.
                msg = (_('manage_existing_snapshot: Error managing '
                         'existing replay %(ss)s on volume %(vol)s') %
                       {'ss': screplay.get('description'),
                        'vol': volume_name})
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)

            # Life is good.  Let the world know what we've done.
            LOG.info(_LI('manage_existing_snapshot: snapshot %(exist)s on '
                         'volume %(volume)s has been renamed to %(id)s and is '
                         'now managed by Cinder.'),
                     {'exist': screplay.get('description'),
                      'volume': volume_name,
                      'id': snapshot_id})
            return {'provider_id': screplay['createVolume']['instanceId']}

    # NOTE: Can't use abstractmethod before all drivers implement it
    def manage_existing_snapshot_get_size(self, snapshot, existing_ref):
        """Return size of snapshot to be managed by manage_existing.

        When calculating the size, round up to the next GB.
        """
        volume_name = snapshot.get('volume_id')
        provider_id = snapshot.get('provider_id')
        with self._client.open_connection() as api:
            screplay = self._get_unmanaged_replay(api, volume_name,
                                                  provider_id, existing_ref)
            sz, rem = dell_storagecenter_api.StorageCenterApi.size_to_gb(
                screplay['size'])
            if rem > 0:
                raise exception.VolumeBackendAPIException(
                    data=_('Volume size must be a multiple of 1 GB.'))
            return sz

    # NOTE: Can't use abstractmethod before all drivers implement it
    def unmanage_snapshot(self, snapshot):
        """Removes the specified snapshot from Cinder management.

        Does not delete the underlying backend storage object.

        NOTE: We do set the expire countdown to 1 day. Once a snapshot is
              unmanaged it will expire 24 hours later.
        """
        with self._client.open_connection() as api:
            snapshot_id = snapshot.get('id')
            # provider_id is the snapshot's parent volume's instanceId.
            provider_id = snapshot.get('provider_id')
            volume_name = snapshot.get('volume_id')
            # Find our volume.
            scvolume = api.find_volume(volume_name, provider_id)
            if not scvolume:
                # Didn't find it.
                msg = (_('unmanage_snapshot: Cannot find volume id %s')
                       % volume_name)
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)
            # Find our replay.
            screplay = api.find_replay(scvolume, snapshot_id)
            if not screplay:
                # Didn't find it. Reference must be invalid.
                msg = (_('unmanage_snapshot: Cannot find snapshot named %s')
                       % snapshot_id)
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)
            # Free our snapshot.
            api.unmanage_replay(screplay)
            # Do not check our result.
