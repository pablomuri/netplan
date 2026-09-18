import os

from .base import TestBase

BASE = '''network:
  version: 2
  ethernets:
    eth0:
      addresses: [10.10.0.1/24]
    eth1:
      addresses: [10.11.0.1/24]
      routes: [{to: 10.21.0.0/24, via: 10.11.0.254}]
'''


class TestIdempotentGenerate(TestBase):

    def _path(self, name, subdir=('run', 'systemd', 'network')):
        return os.path.join(self.workdir.name, *subdir, name)

    def _stat(self, name):
        st = os.stat(self._path(name))
        return (st.st_ino, st.st_mtime_ns)

    def _age(self, *names):
        # push mtimes into the past so any rewrite, even in place, is unambiguous
        for name in names:
            os.utime(self._path(name), ns=(1_000_000_000, 1_000_000_000))

    def test_unchanged_files_keep_inode_and_mtime(self):
        self.generate(BASE)
        self._age('10-netplan-eth0.network', '10-netplan-eth1.network')
        eth0, eth1 = self._stat('10-netplan-eth0.network'), self._stat('10-netplan-eth1.network')
        self.generate(BASE)
        self.assertEqual(self._stat('10-netplan-eth0.network'), eth0)
        self.assertEqual(self._stat('10-netplan-eth1.network'), eth1)

    def test_only_changed_file_is_rewritten(self):
        self.generate(BASE)
        self._age('10-netplan-eth0.network', '10-netplan-eth1.network')
        eth0, eth1 = self._stat('10-netplan-eth0.network'), self._stat('10-netplan-eth1.network')
        self.generate(BASE.replace('10.21.0.0/24', '10.30.0.0/24'))  # same length: content, not size, must differ
        self.assertEqual(self._stat('10-netplan-eth0.network'), eth0)
        self.assertNotEqual(self._stat('10-netplan-eth1.network'), eth1)
        self.assert_networkd({'eth0.network': '''[Match]
Name=eth0

[Network]
LinkLocalAddressing=ipv6
Address=10.10.0.1/24
''',
                              'eth1.network': '''[Match]
Name=eth1

[Network]
LinkLocalAddressing=ipv6
Address=10.11.0.1/24

[Route]
Destination=10.30.0.0/24
Gateway=10.11.0.254
'''})

    def test_removed_vlan_drops_netdev_and_network(self):
        self.generate(BASE + '''  vlans:
    eth0.100:
      id: 100
      link: eth0
      addresses: [10.12.0.1/24]
''')
        self._age('10-netplan-eth0.network', '10-netplan-eth1.network')
        eth0, eth1 = self._stat('10-netplan-eth0.network'), self._stat('10-netplan-eth1.network')
        self.assertTrue(os.path.exists(self._path('10-netplan-eth0.100.netdev')))
        self.generate(BASE)
        # eth0's .network listed the VLAN and is rewritten without it; eth1 is untouched
        self.assertNotEqual(self._stat('10-netplan-eth0.network'), eth0)
        self.assertNotIn('VLAN=', open(self._path('10-netplan-eth0.network')).read())
        self.assertEqual(self._stat('10-netplan-eth1.network'), eth1)
        for name in ('10-netplan-eth0.100.netdev', '10-netplan-eth0.100.network'):
            self.assertFalse(os.path.exists(os.path.join(self.workdir.name, 'run', 'systemd', 'network', name)))

    def test_tampered_file_is_restored(self):
        self.generate(BASE)
        with open(self._path('10-netplan-eth1.network'), 'a') as f:
            f.write('\n[Route]\nDestination=192.0.2.0/24\nGateway=10.11.0.254\n')
        self._age('10-netplan-eth0.network', '10-netplan-eth1.network')
        eth0, eth1 = self._stat('10-netplan-eth0.network'), self._stat('10-netplan-eth1.network')
        self.generate(BASE)  # same YAML: on-disk content, not the YAML, decides what is rewritten
        self.assertEqual(self._stat('10-netplan-eth0.network'), eth0)
        self.assertNotEqual(self._stat('10-netplan-eth1.network'), eth1)
        self.assertNotIn('192.0.2.0/24', open(self._path('10-netplan-eth1.network')).read())

    def test_skipped_write_repairs_mode(self):
        self.generate(BASE)
        os.chmod(self._path('10-netplan-eth0.network'), 0o644)
        self._age('10-netplan-eth0.network')
        eth0 = self._stat('10-netplan-eth0.network')
        self.generate(BASE)
        self.assertEqual(self._stat('10-netplan-eth0.network'), eth0)
        self.assertEqual(os.stat(self._path('10-netplan-eth0.network')).st_mode & 0o777, 0o640)

    def test_link_file_of_physical_nic_is_kept(self):
        yaml = '''network:
  version: 2
  ethernets:
    nic0:
      match: {macaddress: "00:11:22:33:44:55"}
      set-name: nic0
      mtu: 9000
      addresses: [10.10.0.1/24]
'''
        self.generate(yaml)
        self._age('10-netplan-nic0.link', '10-netplan-nic0.network')
        link, network = self._stat('10-netplan-nic0.link'), self._stat('10-netplan-nic0.network')
        self.generate(yaml)
        self.assertEqual(self._stat('10-netplan-nic0.link'), link)
        self.assertEqual(self._stat('10-netplan-nic0.network'), network)
        self.generate(yaml.replace('mtu: 9000', 'mtu: 1500'))
        self.assertNotEqual(self._stat('10-netplan-nic0.link'), link)  # MTUBytes= lives in both files
        self.assertNotEqual(self._stat('10-netplan-nic0.network'), network)

    def test_all_config_removed_deletes_everything(self):
        self.generate(BASE)
        self.generate('network:\n  version: 2\n')
        self.assertEqual(os.listdir(os.path.join(self.workdir.name, 'run', 'systemd', 'network')), [])

    def test_stale_files_are_removed(self):
        self.generate(BASE)
        eth0 = self._stat('10-netplan-eth0.network')
        self.generate('''network:
  version: 2
  ethernets:
    eth0:
      addresses: [10.10.0.1/24]
''')
        self.assertEqual(self._stat('10-netplan-eth0.network'), eth0)
        self.assertFalse(os.path.exists(os.path.join(self.workdir.name, 'run', 'systemd', 'network',
                                                     '10-netplan-eth1.network')))
