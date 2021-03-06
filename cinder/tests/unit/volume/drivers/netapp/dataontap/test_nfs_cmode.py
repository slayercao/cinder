# Copyright (c) 2014 Andrew Kerr.  All rights reserved.
# Copyright (c) 2015 Tom Barron.  All rights reserved.
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
"""
Mock unit tests for the NetApp cmode nfs storage driver
"""

import ddt
import mock
from os_brick.remotefs import remotefs as remotefs_brick
from oslo_service import loopingcall
from oslo_utils import units

from cinder import exception
from cinder.image import image_utils
from cinder import test
from cinder.tests.unit.volume.drivers.netapp.dataontap import fakes as fake
from cinder.tests.unit.volume.drivers.netapp.dataontap.utils import fakes as \
    fake_ssc
from cinder.tests.unit.volume.drivers.netapp import fakes as na_fakes
from cinder import utils
from cinder.volume.drivers.netapp.dataontap.client import api as netapp_api
from cinder.volume.drivers.netapp.dataontap.client import client_cmode
from cinder.volume.drivers.netapp.dataontap import nfs_base
from cinder.volume.drivers.netapp.dataontap import nfs_cmode
from cinder.volume.drivers.netapp.dataontap.performance import perf_cmode
from cinder.volume.drivers.netapp import utils as na_utils
from cinder.volume.drivers import nfs
from cinder.volume import utils as volume_utils


@ddt.ddt
class NetAppCmodeNfsDriverTestCase(test.TestCase):
    def setUp(self):
        super(NetAppCmodeNfsDriverTestCase, self).setUp()

        kwargs = {'configuration': self.get_config_cmode()}

        with mock.patch.object(utils, 'get_root_helper',
                               return_value=mock.Mock()):
            with mock.patch.object(remotefs_brick, 'RemoteFsClient',
                                   return_value=mock.Mock()):
                self.driver = nfs_cmode.NetAppCmodeNfsDriver(**kwargs)
                self.driver._mounted_shares = [fake.NFS_SHARE]
                self.driver.ssc_vols = True
                self.driver.vserver = fake.VSERVER_NAME
                self.driver.ssc_enabled = True
                self.driver.perf_library = mock.Mock()
                self.driver.ssc_library = mock.Mock()
                self.driver.zapi_client = mock.Mock()

    def get_config_cmode(self):
        config = na_fakes.create_configuration_cmode()
        config.netapp_storage_protocol = 'nfs'
        config.netapp_login = 'admin'
        config.netapp_password = 'pass'
        config.netapp_server_hostname = '127.0.0.1'
        config.netapp_transport_type = 'http'
        config.netapp_server_port = '80'
        config.netapp_vserver = fake.VSERVER_NAME
        config.netapp_copyoffload_tool_path = 'copyoffload_tool_path'
        return config

    @mock.patch.object(perf_cmode, 'PerformanceCmodeLibrary', mock.Mock())
    @mock.patch.object(client_cmode, 'Client', mock.Mock())
    @mock.patch.object(nfs.NfsDriver, 'do_setup')
    @mock.patch.object(na_utils, 'check_flags')
    def test_do_setup(self, mock_check_flags, mock_super_do_setup):
        self.driver.do_setup(mock.Mock())

        self.assertTrue(mock_check_flags.called)
        self.assertTrue(mock_super_do_setup.called)

    def test__update_volume_stats(self):
        mock_debug_log = self.mock_object(nfs_cmode.LOG, 'debug')
        self.mock_object(self.driver, 'get_filter_function')
        self.mock_object(self.driver, 'get_goodness_function')
        self.mock_object(self.driver, '_spawn_clean_cache_job')
        self.driver.zapi_client = mock.Mock()
        self.mock_object(
            self.driver, '_get_pool_stats', mock.Mock(return_value={}))
        expected_stats = {
            'driver_version': self.driver.VERSION,
            'pools': {},
            'sparse_copy_volume': True,
            'storage_protocol': 'nfs',
            'vendor_name': 'NetApp',
            'volume_backend_name': 'NetApp_NFS_Cluster_direct',
        }

        retval = self.driver._update_volume_stats()

        self.assertIsNone(retval)
        self.assertTrue(self.driver._spawn_clean_cache_job.called)
        self.assertEqual(1, mock_debug_log.call_count)
        self.assertEqual(expected_stats, self.driver._stats)

    def test_get_pool_stats(self):

        self.driver.zapi_client = mock.Mock()
        ssc = {
            'vola': {
                'pool_name': '10.10.10.10:/vola',
                'thick_provisioning_support': True,
                'thin_provisioning_support': False,
                'netapp_thin_provisioned': 'false',
                'netapp_compression': 'false',
                'netapp_mirrored': 'false',
                'netapp_dedup': 'true',
                'aggregate': 'aggr1',
                'netapp_raid_type': 'raid_dp',
                'netapp_disk_type': 'SSD',
            },
        }
        mock_get_ssc = self.mock_object(self.driver.ssc_library,
                                        'get_ssc',
                                        mock.Mock(return_value=ssc))
        mock_get_aggrs = self.mock_object(self.driver.ssc_library,
                                          'get_ssc_aggregates',
                                          mock.Mock(return_value=['aggr1']))

        total_capacity_gb = na_utils.round_down(
            fake.TOTAL_BYTES // units.Gi, '0.01')
        free_capacity_gb = na_utils.round_down(
            fake.AVAILABLE_BYTES // units.Gi, '0.01')
        provisioned_capacity_gb = total_capacity_gb - free_capacity_gb
        capacity = {
            'reserved_percentage': fake.RESERVED_PERCENTAGE,
            'max_over_subscription_ratio': fake.MAX_OVER_SUBSCRIPTION_RATIO,
            'total_capacity_gb': total_capacity_gb,
            'free_capacity_gb': free_capacity_gb,
            'provisioned_capacity_gb': provisioned_capacity_gb,
        }
        self.mock_object(self.driver,
                         '_get_share_capacity_info',
                         mock.Mock(return_value=capacity))

        aggr_capacities = {
            'aggr1': {
                'percent-used': 45,
                'size-available': 59055800320.0,
                'size-total': 107374182400.0,
            },
        }
        mock_get_aggr_capacities = self.mock_object(
            self.driver.zapi_client, 'get_aggregate_capacities',
            mock.Mock(return_value=aggr_capacities))

        self.driver.perf_library.get_node_utilization_for_pool = (
            mock.Mock(return_value=30.0))

        result = self.driver._get_pool_stats(filter_function='filter',
                                             goodness_function='goodness')

        expected = [{
            'pool_name': '10.10.10.10:/vola',
            'QoS_support': True,
            'reserved_percentage': fake.RESERVED_PERCENTAGE,
            'max_over_subscription_ratio': fake.MAX_OVER_SUBSCRIPTION_RATIO,
            'total_capacity_gb': total_capacity_gb,
            'free_capacity_gb': free_capacity_gb,
            'provisioned_capacity_gb': provisioned_capacity_gb,
            'aggregate_used_percent': 45,
            'utilization': 30.0,
            'filter_function': 'filter',
            'goodness_function': 'goodness',
            'thick_provisioning_support': True,
            'thin_provisioning_support': False,
            'netapp_thin_provisioned': 'false',
            'netapp_compression': 'false',
            'netapp_mirrored': 'false',
            'netapp_dedup': 'true',
            'aggregate': 'aggr1',
            'netapp_raid_type': 'raid_dp',
            'netapp_disk_type': 'SSD',
        }]

        self.assertEqual(expected, result)
        mock_get_ssc.assert_called_once_with()
        mock_get_aggrs.assert_called_once_with()
        mock_get_aggr_capacities.assert_called_once_with(['aggr1'])

    @ddt.data({}, None)
    def test_get_pool_stats_no_ssc_vols(self, ssc):

        mock_get_ssc = self.mock_object(self.driver.ssc_library,
                                        'get_ssc',
                                        mock.Mock(return_value=ssc))

        pools = self.driver._get_pool_stats()

        self.assertListEqual([], pools)
        mock_get_ssc.assert_called_once_with()

    def test_update_ssc(self):

        mock_ensure_shares_mounted = self.mock_object(
            self.driver, '_ensure_shares_mounted')
        mock_get_pool_map = self.mock_object(
            self.driver, '_get_flexvol_to_pool_map',
            mock.Mock(return_value='fake_map'))
        mock_update_ssc = self.mock_object(
            self.driver.ssc_library, 'update_ssc')

        result = self.driver._update_ssc()

        self.assertIsNone(result)
        mock_ensure_shares_mounted.assert_called_once_with()
        mock_get_pool_map.assert_called_once_with()
        mock_update_ssc.assert_called_once_with('fake_map')

    def test_get_pool_map(self):

        self.driver.zapi_client = mock.Mock()
        mock_get_operational_lif_addresses = self.mock_object(
            self.driver.zapi_client, 'get_operational_lif_addresses',
            mock.Mock(return_value=[fake.SHARE_IP]))
        mock_resolve_hostname = self.mock_object(
            na_utils, 'resolve_hostname',
            mock.Mock(return_value=fake.SHARE_IP))
        mock_get_flexvol = self.mock_object(
            self.driver.zapi_client, 'get_flexvol',
            mock.Mock(return_value={'name': fake.NETAPP_VOLUME}))

        result = self.driver._get_flexvol_to_pool_map()

        expected = {
            fake.NETAPP_VOLUME: {
                'pool_name': fake.NFS_SHARE,
            },
        }
        self.assertEqual(expected, result)
        mock_get_operational_lif_addresses.assert_called_once_with()
        mock_resolve_hostname.assert_called_once_with(fake.SHARE_IP)
        mock_get_flexvol.assert_called_once_with(flexvol_path=fake.EXPORT_PATH)

    def test_get_pool_map_address_not_found(self):

        self.driver.zapi_client = mock.Mock()
        self.mock_object(self.driver.zapi_client,
                         'get_operational_lif_addresses',
                         mock.Mock(return_value=[]))
        self.mock_object(na_utils,
                         'resolve_hostname',
                         mock.Mock(return_value=fake.SHARE_IP))

        result = self.driver._get_flexvol_to_pool_map()

        self.assertEqual({}, result)

    def test_get_pool_map_flexvol_not_found(self):

        self.driver.zapi_client = mock.Mock()
        self.mock_object(self.driver.zapi_client,
                         'get_operational_lif_addresses',
                         mock.Mock(return_value=[fake.SHARE_IP]))
        self.mock_object(na_utils,
                         'resolve_hostname',
                         mock.Mock(return_value=fake.SHARE_IP))
        side_effect = exception.VolumeBackendAPIException(data='fake_data')
        self.mock_object(self.driver.zapi_client,
                         'get_flexvol',
                         mock.Mock(side_effect=side_effect))

        result = self.driver._get_flexvol_to_pool_map()

        self.assertEqual({}, result)

    @ddt.data(['/mnt/img-id1', '/mnt/img-id2'], [])
    def test__shortlist_del_eligible_files(self, old_files):
        self.driver.zapi_client = mock.Mock()
        self.driver.zapi_client.get_file_usage = mock.Mock(return_value='1000')
        mock_debug_log = self.mock_object(nfs_cmode.LOG, 'debug')
        self.mock_object(self.driver, '_get_vserver_and_exp_vol',
                         mock.Mock(return_value=('openstack', 'fake_share')))
        expected_list = [(o, '1000') for o in old_files]

        observed_list = self.driver._shortlist_del_eligible_files(
            'fake_ip:fake_share', old_files)

        self.assertEqual(expected_list, observed_list)
        self.assertEqual(1, mock_debug_log.call_count)

    @ddt.data({'ip': None, 'shares': None},
              {'ip': 'fake_ip', 'shares': ['fip:/fsh1']})
    @ddt.unpack
    def test__share_match_for_ip_no_match(self, ip, shares):
        def side_effect(arg):
            if arg == 'fake_ip':
                return 'openstack'
            return None

        self.mock_object(self.driver, '_get_vserver_for_ip',
                         mock.Mock(side_effect=side_effect))
        mock_debug_log = self.mock_object(nfs_cmode.LOG, 'debug')

        retval = self.driver._share_match_for_ip(ip, shares)

        self.assertIsNone(retval)
        self.assertEqual(1, mock_debug_log.call_count)

    def test__share_match_for_ip(self):
        shares = ['fip:/fsh1']
        self.mock_object(self.driver, '_get_vserver_for_ip',
                         mock.Mock(return_value='openstack'))
        mock_debug_log = self.mock_object(nfs_cmode.LOG, 'debug')

        retval = self.driver._share_match_for_ip('fip', shares)

        self.assertEqual('fip:/fsh1', retval)
        self.assertEqual(1, mock_debug_log.call_count)

    def test__get_vserver_for_ip_ignores_zapi_exception(self):
        self.driver.zapi_client = mock.Mock()
        self.driver.zapi_client.get_if_info_by_ip = mock.Mock(
            side_effect=exception.NotFound)

        vserver = self.driver._get_vserver_for_ip('FAKE_IP')

        self.assertIsNone(vserver)

    def test__get_vserver_for_ip(self):
        self.driver.zapi_client = mock.Mock()
        self.driver.zapi_client.get_if_info_by_ip = mock.Mock(
            return_value=fake.get_fake_ifs())

        vserver = self.driver._get_vserver_for_ip('FAKE_IP')

        self.assertIsNone(vserver)

    def test_check_for_setup_error(self):
        super_check_for_setup_error = self.mock_object(
            nfs_base.NetAppNfsDriver, 'check_for_setup_error')
        mock_start_periodic_tasks = self.mock_object(
            self.driver, '_start_periodic_tasks')
        mock_check_api_permissions = self.mock_object(
            self.driver.ssc_library, 'check_api_permissions')

        self.driver.check_for_setup_error()

        self.assertEqual(1, super_check_for_setup_error.call_count)
        mock_check_api_permissions.assert_called_once_with()
        self.assertEqual(1, mock_start_periodic_tasks.call_count)

    def test_delete_volume(self):
        fake_provider_location = 'fake_provider_location'
        fake_volume = {'provider_location': fake_provider_location}
        self.mock_object(self.driver, '_delete_backing_file_for_volume')
        self.mock_object(na_utils,
                         'get_valid_qos_policy_group_info',
                         mock.Mock(return_value='fake_qos_policy_group_info'))

        self.driver.delete_volume(fake_volume)

        self.driver._delete_backing_file_for_volume.assert_called_once_with(
            fake_volume)
        na_utils.get_valid_qos_policy_group_info.assert_called_once_with(
            fake_volume)
        (self.driver.zapi_client.mark_qos_policy_group_for_deletion.
         assert_called_once_with('fake_qos_policy_group_info'))

    def test_delete_volume_exception_path(self):
        fake_provider_location = 'fake_provider_location'
        fake_volume = {'provider_location': fake_provider_location}
        self.mock_object(self.driver, '_delete_backing_file_for_volume')
        self.mock_object(na_utils,
                         'get_valid_qos_policy_group_info',
                         mock.Mock(return_value='fake_qos_policy_group_info'))
        self.mock_object(
            self.driver.zapi_client,
            'mark_qos_policy_group_for_deletion',
            mock.Mock(side_effect=exception.NetAppDriverException))

        self.driver.delete_volume(fake_volume)

        self.driver._delete_backing_file_for_volume.assert_called_once_with(
            fake_volume)
        na_utils.get_valid_qos_policy_group_info.assert_called_once_with(
            fake_volume)
        (self.driver.zapi_client.mark_qos_policy_group_for_deletion.
         assert_called_once_with('fake_qos_policy_group_info'))

    def test_delete_backing_file_for_volume(self):
        mock_filer_delete = self.mock_object(self.driver,
                                             '_delete_volume_on_filer')
        mock_super_delete = self.mock_object(nfs_base.NetAppNfsDriver,
                                             'delete_volume')

        self.driver._delete_backing_file_for_volume(fake.NFS_VOLUME)

        mock_filer_delete.assert_called_once_with(fake.NFS_VOLUME)
        self.assertEqual(0, mock_super_delete.call_count)

    @ddt.data(True, False)
    def test_delete_backing_file_for_volume_exception_path(self, super_exc):
        mock_exception_log = self.mock_object(nfs_cmode.LOG, 'exception')
        exception_call_count = 2 if super_exc else 1
        mock_filer_delete = self.mock_object(self.driver,
                                             '_delete_volume_on_filer')
        mock_filer_delete.side_effect = [Exception]
        mock_super_delete = self.mock_object(nfs_base.NetAppNfsDriver,
                                             'delete_volume')
        if super_exc:
            mock_super_delete.side_effect = [Exception]

        self.driver._delete_backing_file_for_volume(fake.NFS_VOLUME)

        mock_filer_delete.assert_called_once_with(fake.NFS_VOLUME)
        mock_super_delete.assert_called_once_with(fake.NFS_VOLUME)
        self.assertEqual(exception_call_count, mock_exception_log.call_count)

    def test_delete_volume_on_filer(self):
        mock_get_vs_ip = self.mock_object(self.driver, '_get_export_ip_path')
        mock_get_vs_ip.return_value = (fake.VSERVER_NAME, '/%s' % fake.FLEXVOL)
        mock_zapi_delete = self.driver.zapi_client.delete_file

        self.driver._delete_volume_on_filer(fake.NFS_VOLUME)

        mock_zapi_delete.assert_called_once_with(
            '/vol/%s/%s' % (fake.FLEXVOL, fake.NFS_VOLUME['name']))

    def test_delete_snapshot(self):
        mock_get_location = self.mock_object(self.driver,
                                             '_get_provider_location')
        mock_get_location.return_value = fake.PROVIDER_LOCATION
        mock_delete_backing = self.mock_object(
            self.driver, '_delete_backing_file_for_snapshot')

        self.driver.delete_snapshot(fake.test_snapshot)

        mock_delete_backing.assert_called_once_with(fake.test_snapshot)

    def test_delete_backing_file_for_snapshot(self):
        mock_filer_delete = self.mock_object(
            self.driver, '_delete_snapshot_on_filer')
        mock_super_delete = self.mock_object(nfs_base.NetAppNfsDriver,
                                             'delete_snapshot')

        self.driver._delete_backing_file_for_snapshot(fake.test_snapshot)

        mock_filer_delete.assert_called_once_with(fake.test_snapshot)
        self.assertEqual(0, mock_super_delete.call_count)

    @ddt.data(True, False)
    def test_delete_backing_file_for_snapshot_exception_path(self, super_exc):
        mock_exception_log = self.mock_object(nfs_cmode.LOG, 'exception')
        exception_call_count = 2 if super_exc else 1
        mock_filer_delete = self.mock_object(
            self.driver, '_delete_snapshot_on_filer')
        mock_filer_delete.side_effect = [Exception]
        mock_super_delete = self.mock_object(nfs_base.NetAppNfsDriver,
                                             'delete_snapshot')
        if super_exc:
            mock_super_delete.side_effect = [Exception]

        self.driver._delete_backing_file_for_snapshot(fake.test_snapshot)

        mock_filer_delete.assert_called_once_with(fake.test_snapshot)
        mock_super_delete.assert_called_once_with(fake.test_snapshot)
        self.assertEqual(exception_call_count, mock_exception_log.call_count)

    def test_delete_snapshot_on_filer(self):
        mock_get_vs_ip = self.mock_object(self.driver, '_get_export_ip_path')
        mock_get_vs_ip.return_value = (fake.VSERVER_NAME, '/%s' % fake.FLEXVOL)
        mock_zapi_delete = self.driver.zapi_client.delete_file

        self.driver._delete_snapshot_on_filer(fake.test_snapshot)

        mock_zapi_delete.assert_called_once_with(
            '/vol/%s/%s' % (fake.FLEXVOL, fake.test_snapshot['name']))

    def test_do_qos_for_volume_no_exception(self):

        mock_get_info = self.mock_object(na_utils,
                                         'get_valid_qos_policy_group_info')
        mock_get_info.return_value = fake.QOS_POLICY_GROUP_INFO
        mock_provision_qos = self.driver.zapi_client.provision_qos_policy_group
        mock_set_policy = self.mock_object(self.driver,
                                           '_set_qos_policy_group_on_volume')
        mock_error_log = self.mock_object(nfs_cmode.LOG, 'error')
        mock_debug_log = self.mock_object(nfs_cmode.LOG, 'debug')
        mock_cleanup = self.mock_object(self.driver,
                                        '_cleanup_volume_on_failure')

        self.driver._do_qos_for_volume(fake.NFS_VOLUME, fake.EXTRA_SPECS)

        mock_get_info.assert_has_calls([
            mock.call(fake.NFS_VOLUME, fake.EXTRA_SPECS)])
        mock_provision_qos.assert_has_calls([
            mock.call(fake.QOS_POLICY_GROUP_INFO)])
        mock_set_policy.assert_has_calls([
            mock.call(fake.NFS_VOLUME, fake.QOS_POLICY_GROUP_INFO)])
        self.assertEqual(0, mock_error_log.call_count)
        self.assertEqual(0, mock_debug_log.call_count)
        self.assertEqual(0, mock_cleanup.call_count)

    def test_do_qos_for_volume_exception_w_cleanup(self):
        mock_get_info = self.mock_object(na_utils,
                                         'get_valid_qos_policy_group_info')
        mock_get_info.return_value = fake.QOS_POLICY_GROUP_INFO
        mock_provision_qos = self.driver.zapi_client.provision_qos_policy_group
        mock_set_policy = self.mock_object(self.driver,
                                           '_set_qos_policy_group_on_volume')
        mock_set_policy.side_effect = netapp_api.NaApiError
        mock_error_log = self.mock_object(nfs_cmode.LOG, 'error')
        mock_debug_log = self.mock_object(nfs_cmode.LOG, 'debug')
        mock_cleanup = self.mock_object(self.driver,
                                        '_cleanup_volume_on_failure')

        self.assertRaises(netapp_api.NaApiError,
                          self.driver._do_qos_for_volume,
                          fake.NFS_VOLUME,
                          fake.EXTRA_SPECS)

        mock_get_info.assert_has_calls([
            mock.call(fake.NFS_VOLUME, fake.EXTRA_SPECS)])
        mock_provision_qos.assert_has_calls([
            mock.call(fake.QOS_POLICY_GROUP_INFO)])
        mock_set_policy.assert_has_calls([
            mock.call(fake.NFS_VOLUME, fake.QOS_POLICY_GROUP_INFO)])
        self.assertEqual(1, mock_error_log.call_count)
        self.assertEqual(1, mock_debug_log.call_count)
        mock_cleanup.assert_has_calls([
            mock.call(fake.NFS_VOLUME)])

    def test_do_qos_for_volume_exception_no_cleanup(self):

        mock_get_info = self.mock_object(na_utils,
                                         'get_valid_qos_policy_group_info')
        mock_get_info.side_effect = exception.Invalid
        mock_provision_qos = self.driver.zapi_client.provision_qos_policy_group
        mock_set_policy = self.mock_object(self.driver,
                                           '_set_qos_policy_group_on_volume')
        mock_error_log = self.mock_object(nfs_cmode.LOG, 'error')
        mock_debug_log = self.mock_object(nfs_cmode.LOG, 'debug')
        mock_cleanup = self.mock_object(self.driver,
                                        '_cleanup_volume_on_failure')

        self.assertRaises(exception.Invalid, self.driver._do_qos_for_volume,
                          fake.NFS_VOLUME, fake.EXTRA_SPECS, cleanup=False)

        mock_get_info.assert_has_calls([
            mock.call(fake.NFS_VOLUME, fake.EXTRA_SPECS)])
        self.assertEqual(0, mock_provision_qos.call_count)
        self.assertEqual(0, mock_set_policy.call_count)
        self.assertEqual(1, mock_error_log.call_count)
        self.assertEqual(0, mock_debug_log.call_count)
        self.assertEqual(0, mock_cleanup.call_count)

    def test_set_qos_policy_group_on_volume(self):

        mock_get_name_from_info = self.mock_object(
            na_utils, 'get_qos_policy_group_name_from_info')
        mock_get_name_from_info.return_value = fake.QOS_POLICY_GROUP_NAME

        mock_extract_host = self.mock_object(volume_utils, 'extract_host')
        mock_extract_host.return_value = fake.NFS_SHARE

        mock_get_flex_vol_name =\
            self.driver.zapi_client.get_vol_by_junc_vserver
        mock_get_flex_vol_name.return_value = fake.FLEXVOL

        mock_file_assign_qos = self.driver.zapi_client.file_assign_qos

        self.driver._set_qos_policy_group_on_volume(fake.NFS_VOLUME,
                                                    fake.QOS_POLICY_GROUP_INFO)

        mock_get_name_from_info.assert_has_calls([
            mock.call(fake.QOS_POLICY_GROUP_INFO)])
        mock_extract_host.assert_has_calls([
            mock.call(fake.NFS_HOST_STRING, level='pool')])
        mock_get_flex_vol_name.assert_has_calls([
            mock.call(fake.VSERVER_NAME, fake.EXPORT_PATH)])
        mock_file_assign_qos.assert_has_calls([
            mock.call(fake.FLEXVOL, fake.QOS_POLICY_GROUP_NAME,
                      fake.NFS_VOLUME['name'])])

    def test_set_qos_policy_group_on_volume_no_info(self):

        mock_get_name_from_info = self.mock_object(
            na_utils, 'get_qos_policy_group_name_from_info')

        mock_extract_host = self.mock_object(volume_utils, 'extract_host')

        mock_get_flex_vol_name =\
            self.driver.zapi_client.get_vol_by_junc_vserver

        mock_file_assign_qos = self.driver.zapi_client.file_assign_qos

        self.driver._set_qos_policy_group_on_volume(fake.NFS_VOLUME,
                                                    None)

        self.assertEqual(0, mock_get_name_from_info.call_count)
        self.assertEqual(0, mock_extract_host.call_count)
        self.assertEqual(0, mock_get_flex_vol_name.call_count)
        self.assertEqual(0, mock_file_assign_qos.call_count)

    def test_set_qos_policy_group_on_volume_no_name(self):

        mock_get_name_from_info = self.mock_object(
            na_utils, 'get_qos_policy_group_name_from_info')
        mock_get_name_from_info.return_value = None

        mock_extract_host = self.mock_object(volume_utils, 'extract_host')

        mock_get_flex_vol_name =\
            self.driver.zapi_client.get_vol_by_junc_vserver

        mock_file_assign_qos = self.driver.zapi_client.file_assign_qos

        self.driver._set_qos_policy_group_on_volume(fake.NFS_VOLUME,
                                                    fake.QOS_POLICY_GROUP_INFO)

        mock_get_name_from_info.assert_has_calls([
            mock.call(fake.QOS_POLICY_GROUP_INFO)])
        self.assertEqual(0, mock_extract_host.call_count)
        self.assertEqual(0, mock_get_flex_vol_name.call_count)
        self.assertEqual(0, mock_file_assign_qos.call_count)

    @ddt.data({'share': None, 'is_snapshot': False},
              {'share': None, 'is_snapshot': True},
              {'share': 'fake_share', 'is_snapshot': False},
              {'share': 'fake_share', 'is_snapshot': True})
    @ddt.unpack
    def test_clone_backing_file_for_volume(self, share, is_snapshot):

        mock_get_vserver_and_exp_vol = self.mock_object(
            self.driver, '_get_vserver_and_exp_vol',
            mock.Mock(return_value=(fake.VSERVER_NAME, fake.FLEXVOL)))

        self.driver._clone_backing_file_for_volume(
            fake.FLEXVOL, 'fake_clone', fake.VOLUME_ID, share=share,
            is_snapshot=is_snapshot)

        mock_get_vserver_and_exp_vol.assert_called_once_with(
            fake.VOLUME_ID, share)
        self.driver.zapi_client.clone_file.assert_called_once_with(
            fake.FLEXVOL, fake.FLEXVOL, 'fake_clone', fake.VSERVER_NAME,
            is_snapshot=is_snapshot)

    def test__clone_backing_file_for_volume(self):
        body = fake.get_fake_net_interface_get_iter_response()
        self.driver.zapi_client.get_if_info_by_ip = mock.Mock(
            return_value=[netapp_api.NaElement(body)])
        self.driver.zapi_client.get_vol_by_junc_vserver = mock.Mock(
            return_value='nfsvol')
        self.mock_object(self.driver, '_get_export_ip_path',
                         mock.Mock(return_value=('127.0.0.1', 'fakepath')))

        retval = self.driver._clone_backing_file_for_volume(
            'vol', 'clone', 'vol_id', share='share', is_snapshot=True)

        self.assertIsNone(retval)
        self.driver.zapi_client.clone_file.assert_called_once_with(
            'nfsvol', 'vol', 'clone', None, is_snapshot=True)

    def test__copy_from_img_service_copyoffload_nonexistent_binary_path(self):
        self.mock_object(nfs_cmode.LOG, 'debug')
        drv = self.driver
        context = object()
        volume = {'id': 'vol_id', 'name': 'name'}
        image_service = mock.Mock()
        image_service.get_location.return_value = (mock.Mock(), mock.Mock())
        image_service.show.return_value = {'size': 0}
        image_id = 'image_id'
        drv._client = mock.Mock()
        drv._client.get_api_version = mock.Mock(return_value=(1, 20))
        drv._find_image_in_cache = mock.Mock(return_value=[])
        drv._construct_image_nfs_url = mock.Mock(return_value=["nfs://1"])
        drv._check_get_nfs_path_segs = mock.Mock(
            return_value=("test:test", "dr"))
        drv._get_ip_verify_on_cluster = mock.Mock(return_value="192.128.1.1")
        drv._get_mount_point_for_share = mock.Mock(return_value='mnt_point')
        drv._get_host_ip = mock.Mock()
        drv._get_provider_location = mock.Mock()
        drv._get_export_path = mock.Mock(return_value="dr")
        drv._check_share_can_hold_size = mock.Mock()
        # Raise error as if the copyoffload file can not be found
        drv._clone_file_dst_exists = mock.Mock(side_effect=OSError())
        drv._discover_file_till_timeout = mock.Mock()

        # Verify the original error is propagated
        self.assertRaises(OSError, drv._copy_from_img_service,
                          context, volume, image_service, image_id)

        drv._discover_file_till_timeout.assert_not_called()

    @mock.patch.object(image_utils, 'qemu_img_info')
    def test__copy_from_img_service_raw_copyoffload_workflow_success(
            self, mock_qemu_img_info):
        drv = self.driver
        volume = {'id': 'vol_id', 'name': 'name', 'size': 1}
        image_id = 'image_id'
        context = object()
        image_service = mock.Mock()
        image_service.get_location.return_value = ('nfs://ip1/openstack/img',
                                                   None)
        image_service.show.return_value = {'size': 1, 'disk_format': 'raw'}

        drv._check_get_nfs_path_segs =\
            mock.Mock(return_value=('ip1', '/openstack'))
        drv._get_ip_verify_on_cluster = mock.Mock(return_value='ip1')
        drv._get_host_ip = mock.Mock(return_value='ip2')
        drv._get_export_path = mock.Mock(return_value='/exp_path')
        drv._get_provider_location = mock.Mock(return_value='share')
        drv._execute = mock.Mock()
        drv._get_mount_point_for_share = mock.Mock(return_value='mnt_point')
        drv._discover_file_till_timeout = mock.Mock(return_value=True)
        img_inf = mock.Mock()
        img_inf.file_format = 'raw'
        mock_qemu_img_info.return_value = img_inf
        drv._check_share_can_hold_size = mock.Mock()
        drv._move_nfs_file = mock.Mock(return_value=True)
        drv._delete_file_at_path = mock.Mock()
        drv._clone_file_dst_exists = mock.Mock()
        drv._post_clone_image = mock.Mock()

        retval = drv._copy_from_img_service(
            context, volume, image_service, image_id)

        self.assertIsNone(retval)
        drv._get_ip_verify_on_cluster.assert_any_call('ip1')
        drv._get_export_path.assert_called_with('vol_id')
        drv._check_share_can_hold_size.assert_called_with('share', 1)
        drv._post_clone_image.assert_called_with(volume)
        self.assertEqual(1, drv._execute.call_count)

    @mock.patch.object(image_utils, 'convert_image')
    @mock.patch.object(image_utils, 'qemu_img_info')
    @mock.patch('os.path.exists')
    def test__copy_from_img_service_qcow2_copyoffload_workflow_success(
            self, mock_exists, mock_qemu_img_info, mock_cvrt_image):
        drv = self.driver
        volume = {'id': 'vol_id', 'name': 'name', 'size': 1}
        image_id = 'image_id'
        context = object()
        image_service = mock.Mock()
        image_service.get_location.return_value = ('nfs://ip1/openstack/img',
                                                   None)
        image_service.show.return_value = {'size': 1,
                                           'disk_format': 'qcow2'}
        drv._check_get_nfs_path_segs =\
            mock.Mock(return_value=('ip1', '/openstack'))

        drv._get_ip_verify_on_cluster = mock.Mock(return_value='ip1')
        drv._get_host_ip = mock.Mock(return_value='ip2')
        drv._get_export_path = mock.Mock(return_value='/exp_path')
        drv._get_provider_location = mock.Mock(return_value='share')
        drv._execute = mock.Mock()
        drv._get_mount_point_for_share = mock.Mock(return_value='mnt_point')
        img_inf = mock.Mock()
        img_inf.file_format = 'raw'
        mock_qemu_img_info.return_value = img_inf
        drv._check_share_can_hold_size = mock.Mock()

        drv._move_nfs_file = mock.Mock(return_value=True)
        drv._delete_file_at_path = mock.Mock()
        drv._clone_file_dst_exists = mock.Mock()
        drv._post_clone_image = mock.Mock()

        retval = drv._copy_from_img_service(
            context, volume, image_service, image_id)

        self.assertIsNone(retval)
        drv._get_ip_verify_on_cluster.assert_any_call('ip1')
        drv._get_export_path.assert_called_with('vol_id')
        drv._check_share_can_hold_size.assert_called_with('share', 1)
        drv._post_clone_image.assert_called_with(volume)
        self.assertEqual(1, mock_cvrt_image.call_count)
        self.assertEqual(1, drv._execute.call_count)
        self.assertEqual(2, drv._delete_file_at_path.call_count)
        self.assertEqual(1, drv._clone_file_dst_exists.call_count)

    def test__copy_from_cache_copyoffload_success(self):
        drv = self.driver
        volume = {'id': 'vol_id', 'name': 'name', 'size': 1}
        image_id = 'image_id'
        cache_result = [('ip1:/openstack', 'img-cache-imgid')]
        drv._get_ip_verify_on_cluster = mock.Mock(return_value='ip1')
        drv._get_host_ip = mock.Mock(return_value='ip2')
        drv._get_export_path = mock.Mock(return_value='/exp_path')
        drv._execute = mock.Mock()
        drv._register_image_in_cache = mock.Mock()
        drv._get_provider_location = mock.Mock(return_value='/share')
        drv._post_clone_image = mock.Mock()

        copied = drv._copy_from_cache(volume, image_id, cache_result)

        self.assertTrue(copied)
        drv._get_ip_verify_on_cluster.assert_any_call('ip1')
        drv._get_export_path.assert_called_with('vol_id')
        drv._execute.assert_called_once_with(
            'copyoffload_tool_path', 'ip1', 'ip1',
            '/openstack/img-cache-imgid', '/exp_path/name',
            run_as_root=False, check_exit_code=0)
        drv._post_clone_image.assert_called_with(volume)
        drv._get_provider_location.assert_called_with('vol_id')

    def test_unmanage(self):
        mock_get_info = self.mock_object(na_utils,
                                         'get_valid_qos_policy_group_info')
        mock_get_info.return_value = fake.QOS_POLICY_GROUP_INFO

        mock_mark_for_deletion =\
            self.driver.zapi_client.mark_qos_policy_group_for_deletion

        super_unmanage = self.mock_object(nfs_base.NetAppNfsDriver, 'unmanage')

        self.driver.unmanage(fake.NFS_VOLUME)

        mock_get_info.assert_has_calls([mock.call(fake.NFS_VOLUME)])
        mock_mark_for_deletion.assert_has_calls([
            mock.call(fake.QOS_POLICY_GROUP_INFO)])
        super_unmanage.assert_has_calls([mock.call(fake.NFS_VOLUME)])

    def test_unmanage_invalid_qos(self):
        mock_get_info = self.mock_object(na_utils,
                                         'get_valid_qos_policy_group_info')
        mock_get_info.side_effect = exception.Invalid

        super_unmanage = self.mock_object(nfs_base.NetAppNfsDriver, 'unmanage')

        self.driver.unmanage(fake.NFS_VOLUME)

        mock_get_info.assert_has_calls([mock.call(fake.NFS_VOLUME)])
        super_unmanage.assert_has_calls([mock.call(fake.NFS_VOLUME)])

    def test_start_periodic_tasks(self):

        mock_update_ssc = self.mock_object(self.driver, '_update_ssc')
        mock_remove_unused_qos_policy_groups = self.mock_object(
            self.driver.zapi_client,
            'remove_unused_qos_policy_groups')

        update_ssc_periodic_task = mock.Mock()
        harvest_qos_periodic_task = mock.Mock()
        side_effect = [update_ssc_periodic_task, harvest_qos_periodic_task]
        mock_loopingcall = self.mock_object(
            loopingcall, 'FixedIntervalLoopingCall',
            mock.Mock(side_effect=side_effect))

        self.driver._start_periodic_tasks()

        mock_loopingcall.assert_has_calls([
            mock.call(mock_update_ssc),
            mock.call(mock_remove_unused_qos_policy_groups)])
        self.assertTrue(update_ssc_periodic_task.start.called)
        self.assertTrue(harvest_qos_periodic_task.start.called)
        mock_update_ssc.assert_called_once_with()

    @ddt.data({'has_space': True, 'type_match': True, 'expected': True},
              {'has_space': True, 'type_match': False, 'expected': False},
              {'has_space': False, 'type_match': True, 'expected': False},
              {'has_space': False, 'type_match': False, 'expected': False})
    @ddt.unpack
    def test_is_share_clone_compatible(self, has_space, type_match, expected):

        mock_get_flexvol_name_for_share = self.mock_object(
            self.driver, '_get_flexvol_name_for_share',
            mock.Mock(return_value='fake_flexvol'))
        mock_is_volume_thin_provisioned = self.mock_object(
            self.driver, '_is_volume_thin_provisioned',
            mock.Mock(return_value='thin'))
        mock_share_has_space_for_clone = self.mock_object(
            self.driver, '_share_has_space_for_clone',
            mock.Mock(return_value=has_space))
        mock_is_share_vol_type_match = self.mock_object(
            self.driver, '_is_share_vol_type_match',
            mock.Mock(return_value=type_match))

        result = self.driver._is_share_clone_compatible(fake.VOLUME,
                                                        fake.NFS_SHARE)

        self.assertEqual(expected, result)
        mock_get_flexvol_name_for_share.assert_called_once_with(fake.NFS_SHARE)
        mock_is_volume_thin_provisioned.assert_called_once_with('fake_flexvol')
        mock_share_has_space_for_clone.assert_called_once_with(
            fake.NFS_SHARE, fake.SIZE, 'thin')
        if has_space:
            mock_is_share_vol_type_match.assert_called_once_with(
                fake.VOLUME, fake.NFS_SHARE, 'fake_flexvol')

    @ddt.data({'thin': True, 'expected': True},
              {'thin': False, 'expected': False},
              {'thin': None, 'expected': False})
    @ddt.unpack
    def test_is_volume_thin_provisioned(self, thin, expected):

        ssc_data = {'thin_provisioning_support': thin}
        mock_get_ssc_for_flexvol = self.mock_object(
            self.driver.ssc_library, 'get_ssc_for_flexvol',
            mock.Mock(return_value=ssc_data))

        result = self.driver._is_volume_thin_provisioned('fake_flexvol')

        self.assertEqual(expected, result)
        mock_get_ssc_for_flexvol.assert_called_once_with('fake_flexvol')

    @ddt.data({'flexvols': ['volume1', 'volume2'], 'expected': True},
              {'flexvols': ['volume3', 'volume4'], 'expected': False},
              {'flexvols': [], 'expected': False})
    @ddt.unpack
    def test_is_share_vol_type_match(self, flexvols, expected):

        mock_get_volume_extra_specs = self.mock_object(
            na_utils, 'get_volume_extra_specs',
            mock.Mock(return_value='fake_extra_specs'))
        mock_get_matching_flexvols_for_extra_specs = self.mock_object(
            self.driver.ssc_library, 'get_matching_flexvols_for_extra_specs',
            mock.Mock(return_value=flexvols))

        result = self.driver._is_share_vol_type_match(fake.VOLUME,
                                                      fake.NFS_SHARE,
                                                      'volume1')

        self.assertEqual(expected, result)
        mock_get_volume_extra_specs.assert_called_once_with(fake.VOLUME)
        mock_get_matching_flexvols_for_extra_specs.assert_called_once_with(
            'fake_extra_specs')

    @ddt.data({'share': 'volume1', 'expected': 'volume1'},
              {'share': 'volume3', 'expected': None})
    @ddt.unpack
    def test_get_flexvol_name_for_share(self, share, expected):

        mock_get_ssc = self.mock_object(
            self.driver.ssc_library, 'get_ssc',
            mock.Mock(return_value=fake_ssc.SSC))

        result = self.driver._get_flexvol_name_for_share(share)

        self.assertEqual(expected, result)
        mock_get_ssc.assert_called_once_with()

    def test_get_flexvol_name_for_share_no_ssc_vols(self):

        mock_get_ssc = self.mock_object(
            self.driver.ssc_library, 'get_ssc',
            mock.Mock(return_value={}))

        result = self.driver._get_flexvol_name_for_share('fake_share')

        self.assertIsNone(result)
        mock_get_ssc.assert_called_once_with()

    def test_find_image_location_with_local_copy(self):
        local_share = '/share'
        cache_result = [
            ('ip1:/openstack', 'img-cache-imgid'),
            ('ip2:/openstack', 'img-cache-imgid'),
            (local_share, 'img-cache-imgid'),
            ('ip3:/openstack', 'img-cache-imgid'),
        ]
        self.driver._get_provider_location = mock.Mock(
            return_value=local_share)

        cache_copy, found_local_copy = self.driver._find_image_location(
            cache_result, fake.VOLUME_ID)

        self.assertEqual(cache_result[2], cache_copy)
        self.assertTrue(found_local_copy)
        self.driver._get_provider_location.assert_called_once_with(
            fake.VOLUME_ID)

    def test_find_image_location_with_remote_copy(self):
        cache_result = [('ip1:/openstack', 'img-cache-imgid')]
        self.driver._get_provider_location = mock.Mock(return_value='/share')

        cache_copy, found_local_copy = self.driver._find_image_location(
            cache_result, fake.VOLUME_ID)

        self.assertEqual(cache_result[0], cache_copy)
        self.assertFalse(found_local_copy)
        self.driver._get_provider_location.assert_called_once_with(
            fake.VOLUME_ID)

    def test_find_image_location_without_cache_copy(self):
        cache_result = []
        self.driver._get_provider_location = mock.Mock(return_value='/share')

        cache_copy, found_local_copy = self.driver._find_image_location(
            cache_result, fake.VOLUME_ID)

        self.assertIsNone(cache_copy)
        self.assertFalse(found_local_copy)
        self.driver._get_provider_location.assert_called_once_with(
            fake.VOLUME_ID)

    def test_clone_file_dest_exists(self):
        self.driver._get_vserver_and_exp_vol = mock.Mock(
            return_value=(fake.VSERVER_NAME, fake.EXPORT_PATH))
        self.driver.zapi_client.clone_file = mock.Mock()

        self.driver._clone_file_dst_exists(
            fake.NFS_SHARE, fake.IMAGE_FILE_ID, fake.VOLUME['name'],
            dest_exists=True)

        self.driver._get_vserver_and_exp_vol.assert_called_once_with(
            share=fake.NFS_SHARE)
        self.driver.zapi_client.clone_file.assert_called_once_with(
            fake.EXPORT_PATH, fake.IMAGE_FILE_ID, fake.VOLUME['name'],
            fake.VSERVER_NAME, dest_exists=True)

    def test_get_source_ip_and_path(self):
        self.driver._get_ip_verify_on_cluster = mock.Mock(
            return_value=fake.SHARE_IP)

        src_ip, src_path = self.driver._get_source_ip_and_path(
            fake.NFS_SHARE, fake.IMAGE_FILE_ID)

        self.assertEqual(fake.SHARE_IP, src_ip)
        assert_path = fake.EXPORT_PATH + '/' + fake.IMAGE_FILE_ID
        self.assertEqual(assert_path, src_path)
        self.driver._get_ip_verify_on_cluster.assert_called_once_with(
            fake.SHARE_IP)

    def test_get_destination_ip_and_path(self):
        self.driver._get_ip_verify_on_cluster = mock.Mock(
            return_value=fake.SHARE_IP)
        self.driver._get_host_ip = mock.Mock(return_value='host.ip')
        self.driver._get_export_path = mock.Mock(return_value=fake.EXPORT_PATH)

        dest_ip, dest_path = self.driver._get_destination_ip_and_path(
            fake.VOLUME)

        self.assertEqual(fake.SHARE_IP, dest_ip)
        assert_path = fake.EXPORT_PATH + '/' + fake.LUN_NAME
        self.assertEqual(assert_path, dest_path)
        self.driver._get_ip_verify_on_cluster.assert_called_once_with(
            'host.ip')
        self.driver._get_host_ip.assert_called_once_with(fake.VOLUME_ID)
        self.driver._get_export_path.assert_called_once_with(fake.VOLUME_ID)

    def test_copy_image_to_volume_copyoffload_non_cached_ssc_update(self):
        mock_log = self.mock_object(nfs_cmode, 'LOG')
        drv = self.driver
        context = object()
        volume = {'id': 'vol_id', 'name': 'name'}
        image_service = object()
        image_id = 'image_id'
        drv.zapi_client = mock.Mock()
        drv.zapi_client.get_ontapi_version = mock.Mock(return_value=(1, 20))
        drv._copy_from_img_service = mock.Mock()
        drv._get_provider_location = mock.Mock(return_value='share')
        drv._get_vol_for_share = mock.Mock(return_value='vol')

        retval = drv.copy_image_to_volume(
            context, volume, image_service, image_id)

        self.assertIsNone(retval)
        drv._copy_from_img_service.assert_called_once_with(
            context, volume, image_service, image_id)
        self.assertEqual(1, mock_log.debug.call_count)
        self.assertEqual(1, mock_log.info.call_count)

    def test_copy_image_to_volume_copyoffload_from_cache_success(self):
        mock_info_log = self.mock_object(nfs_cmode.LOG, 'info')
        drv = self.driver
        context = object()
        volume = {'id': 'vol_id', 'name': 'name'}
        image_service = object()
        image_id = 'image_id'
        drv.zapi_client = mock.Mock()
        drv.zapi_client.get_ontapi_version = mock.Mock(return_value=(1, 20))
        nfs_base.NetAppNfsDriver.copy_image_to_volume = mock.Mock()
        drv._get_provider_location = mock.Mock(return_value='share')
        drv._get_vol_for_share = mock.Mock(return_value='vol')
        drv._find_image_in_cache = mock.Mock(return_value=[('share', 'img')])
        drv._copy_from_cache = mock.Mock(return_value=True)

        drv.copy_image_to_volume(context, volume, image_service, image_id)

        drv._copy_from_cache.assert_called_once_with(
            volume, image_id, [('share', 'img')])
        self.assertEqual(1, mock_info_log.call_count)

    def test_copy_image_to_volume_copyoffload_from_img_service(self):
        drv = self.driver
        context = object()
        volume = {'id': 'vol_id', 'name': 'name'}
        image_service = object()
        image_id = 'image_id'
        drv.zapi_client = mock.Mock()
        drv.zapi_client.get_ontapi_version = mock.Mock(return_value=(1, 20))
        nfs_base.NetAppNfsDriver.copy_image_to_volume = mock.Mock()
        drv._get_provider_location = mock.Mock(return_value='share')
        drv._get_vol_for_share = mock.Mock(return_value='vol')
        drv._find_image_in_cache = mock.Mock(return_value=False)
        drv._copy_from_img_service = mock.Mock()

        retval = drv.copy_image_to_volume(
            context, volume, image_service, image_id)

        self.assertIsNone(retval)
        drv._copy_from_img_service.assert_called_once_with(
            context, volume, image_service, image_id)

    def test_copy_image_to_volume_copyoffload_failure(self):
        mock_log = self.mock_object(nfs_cmode, 'LOG')
        drv = self.driver
        context = object()
        volume = {'id': 'vol_id', 'name': 'name'}
        image_service = object()
        image_id = 'image_id'
        drv.zapi_client = mock.Mock()
        drv.zapi_client.get_ontapi_version = mock.Mock(return_value=(1, 20))
        drv._copy_from_img_service = mock.Mock(side_effect=Exception())
        nfs_base.NetAppNfsDriver.copy_image_to_volume = mock.Mock()
        drv._get_provider_location = mock.Mock(return_value='share')
        drv._get_vol_for_share = mock.Mock(return_value='vol')

        retval = drv.copy_image_to_volume(
            context, volume, image_service, image_id)

        self.assertIsNone(retval)
        drv._copy_from_img_service.assert_called_once_with(
            context, volume, image_service, image_id)
        nfs_base.NetAppNfsDriver.copy_image_to_volume. \
            assert_called_once_with(context, volume, image_service, image_id)
        mock_log.info.assert_not_called()
        self.assertEqual(1, mock_log.exception.call_count)

    def test_copy_from_remote_cache(self):
        source_ip = '192.0.1.1'
        source_path = '/openstack/img-cache-imgid'
        cache_copy = ('192.0.1.1:/openstack', fake.IMAGE_FILE_ID)
        dest_path = fake.EXPORT_PATH + '/' + fake.VOLUME['name']
        self.driver._execute = mock.Mock()
        self.driver._get_source_ip_and_path = mock.Mock(
            return_value=(source_ip, source_path))
        self.driver._get_destination_ip_and_path = mock.Mock(
            return_value=(fake.SHARE_IP, dest_path))
        self.driver._register_image_in_cache = mock.Mock()

        self.driver._copy_from_remote_cache(
            fake.VOLUME, fake.IMAGE_FILE_ID, cache_copy)

        self.driver._execute.assert_called_once_with(
            'copyoffload_tool_path', source_ip, fake.SHARE_IP,
            source_path, dest_path, run_as_root=False, check_exit_code=0)
        self.driver._get_source_ip_and_path.assert_called_once_with(
            cache_copy[0], fake.IMAGE_FILE_ID)
        self.driver._get_destination_ip_and_path.assert_called_once_with(
            fake.VOLUME)
        self.driver._register_image_in_cache.assert_called_once_with(
            fake.VOLUME, fake.IMAGE_FILE_ID)

    def test_copy_from_cache_workflow_remote_location(self):
        cache_result = [('ip1:/openstack', fake.IMAGE_FILE_ID),
                        ('ip2:/openstack', fake.IMAGE_FILE_ID),
                        ('ip3:/openstack', fake.IMAGE_FILE_ID)]
        self.driver._find_image_location = mock.Mock(return_value=[
            cache_result[0], False])
        self.driver._copy_from_remote_cache = mock.Mock()
        self.driver._post_clone_image = mock.Mock()

        copied = self.driver._copy_from_cache(
            fake.VOLUME, fake.IMAGE_FILE_ID, cache_result)

        self.assertTrue(copied)
        self.driver._copy_from_remote_cache.assert_called_once_with(
            fake.VOLUME, fake.IMAGE_FILE_ID, cache_result[0])
        self.driver._post_clone_image.assert_called_once_with(fake.VOLUME)

    def test_copy_from_cache_workflow_local_location(self):
        local_share = '/share'
        cache_result = [
            ('ip1:/openstack', 'img-cache-imgid'),
            ('ip2:/openstack', 'img-cache-imgid'),
            (local_share, 'img-cache-imgid'),
            ('ip3:/openstack', 'img-cache-imgid'),
        ]
        self.driver._find_image_location = mock.Mock(return_value=[
            cache_result[2], True])
        self.driver._clone_file_dst_exists = mock.Mock()
        self.driver._post_clone_image = mock.Mock()

        copied = self.driver._copy_from_cache(
            fake.VOLUME, fake.IMAGE_FILE_ID, cache_result)

        self.assertTrue(copied)
        self.driver._clone_file_dst_exists.assert_called_once_with(
            local_share, fake.IMAGE_FILE_ID, fake.VOLUME['name'],
            dest_exists=True)
        self.driver._post_clone_image.assert_called_once_with(fake.VOLUME)

    def test_copy_from_cache_workflow_no_location(self):
        cache_result = []
        self.driver._find_image_location = mock.Mock(
            return_value=(None, False))

        copied = self.driver._copy_from_cache(
            fake.VOLUME, fake.IMAGE_FILE_ID, cache_result)

        self.assertFalse(copied)

    def test_copy_from_cache_workflow_exception(self):
        cache_result = [('ip1:/openstack', fake.IMAGE_FILE_ID)]
        self.driver._find_image_location = mock.Mock(return_value=[
            cache_result[0], False])
        self.driver._copy_from_remote_cache = mock.Mock(
            side_effect=Exception)
        self.driver._post_clone_image = mock.Mock()

        copied = self.driver._copy_from_cache(
            fake.VOLUME, fake.IMAGE_FILE_ID, cache_result)

        self.assertFalse(copied)
        self.driver._copy_from_remote_cache.assert_called_once_with(
            fake.VOLUME, fake.IMAGE_FILE_ID, cache_result[0])
        self.assertFalse(self.driver._post_clone_image.called)
