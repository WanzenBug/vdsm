#
# Copyright 2014 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from vdsm.tool import configurator
from vdsm.tool.configurators import \
    NO,\
    MAYBE,\
    YES,\
    InvalidConfig,\
    InvalidRun
from vdsm.tool.configfile import ConfigFile, ParserWrapper
from vdsm.tool.configurators import libvirt
from vdsm.tool import UsageError
from vdsm.tool import upgrade
from vdsm import utils
import monkeypatch
from testlib import VdsmTestCase
from testValidation import ValidateRunningAsRoot
from unittest import TestCase
import tempfile
import os
import shutil

dirName = os.path.dirname(os.path.realpath(__file__))


class MockModuleConfigurator(object):

    def __init__(self, name, requires=(), should_succeed=True):
        self._name = name
        self._requires = frozenset(requires)
        self.should_succeed = should_succeed

    @property
    def name(self):
        return self._name

    @property
    def requires(self):
        return self._requires

    def __repr__(self):
        return "name: %s, requires: %s" % (self._name, self._requires)

    def validate(self):
        return self.should_succeed

    def isconfigured(self):
        if self.should_succeed:
            return YES
        return NO

    def configure(self):
        if not self.should_succeed:
            raise InvalidRun('mock for invalid configure')

    def removeConf(self):
        if not self.should_succeed:
            raise Exception('mock invalid remove conf')


def patchConfigurators(mockConfigurers):
    return monkeypatch.MonkeyPatch(
        configurator,
        '_CONFIGURATORS',
        dict((m.name, m) for m in mockConfigurers))


class PatchConfiguratorsTests(VdsmTestCase):

    def testPatch(self):
        self.configurator = MockModuleConfigurator('a')
        self.function_was_run = False

        @patchConfigurators((self.configurator,))
        def function():
            self.function_was_run = True
            conf = configurator._CONFIGURATORS[self.configurator.name]
            self.assertTrue(conf is self.configurator)

        self.assertFalse(self.configurator.name in configurator._CONFIGURATORS)
        function()
        self.assertTrue(self.function_was_run)
        self.assertFalse(self.configurator.name in configurator._CONFIGURATORS)


class ConfiguratorTests(VdsmTestCase):

    @patchConfigurators(
        (
            MockModuleConfigurator('a', ('b',)),
            MockModuleConfigurator('b', ('a',)),
        )
    )
    def testDependencyCircle(self):
        self.assertRaises(
            RuntimeError,
            configurator._parse_args,
            'validate-config'
        )

    @patchConfigurators(
        (
            MockModuleConfigurator('a', ('b', 'd')),
            MockModuleConfigurator('b', ('c',)),
            MockModuleConfigurator('c', ('e', 'd')),
            MockModuleConfigurator('d', ('e', 'e')),
            MockModuleConfigurator('e'),
            MockModuleConfigurator('f'),
        )
    )
    def testNormalDependencies(self):
        modules = configurator._parse_args('validate-config').modules
        # make sure this is indeed a topological sort.
        before_m = set()
        for m in modules:
            for n in m.requires:
                self.assertIn(n, before_m)
            before_m.add(m.name)

    @patchConfigurators(
        (
            MockModuleConfigurator('a'),
            MockModuleConfigurator('b'),
            MockModuleConfigurator('c'),
        )
    )
    def testNoDependencies(self):
        configurator._parse_args('validate-config').modules

    @patchConfigurators(
        (
            MockModuleConfigurator('a', ('b', 'c')),
            MockModuleConfigurator('b'),
            MockModuleConfigurator('c'),
        )
    )
    def testDependenciesAdditionPositive(self):
        modules = configurator._parse_args(
            'validate-config',
            '--module=a'
        ).modules
        modulesNames = [m.name for m in modules]
        self.assertTrue('a' in modulesNames)
        self.assertTrue('b' in modulesNames)
        self.assertTrue('c' in modulesNames)

    @patchConfigurators(
        (
            MockModuleConfigurator('a'),
            MockModuleConfigurator('b'),
            MockModuleConfigurator('c'),
        )
    )
    def testDependenciesAdditionNegative(self):

        modules = configurator._parse_args(
            'validate-config',
            '--module=a'
        ).modules
        moduleNames = [m.name for m in modules]
        self.assertTrue('a' in moduleNames)
        self.assertFalse('b' in moduleNames)
        self.assertFalse('c' in moduleNames)

    @patchConfigurators(
        (
            MockModuleConfigurator('libvirt'),
            MockModuleConfigurator('sanlock'),
        )
    )
    def testNonExistentModule(self):

        self.assertRaises(
            UsageError,
            configurator._parse_args,
            'validate-config',
            '--module=multipath'
        )

    @ValidateRunningAsRoot
    @patchConfigurators(
        (
            MockModuleConfigurator('a', should_succeed=False),
            MockModuleConfigurator('b', should_succeed=False),

        )
    )
    def testFailuresOnExposedFuncs(self):
        self.assertRaises(
            InvalidConfig,
            configurator.validate_config,
            'validate-config',
        )
        self.assertRaises(
            InvalidRun,
            configurator.isconfigured,
            'is-configured',
        )
        # running configure with force flag to avoid validate check
        self.assertRaises(
            InvalidRun,
            configurator.configure,
            'configure',
            '--force',
        )
        self.assertRaises(
            InvalidRun,
            configurator.remove_config,
            'remove-config'
        )

    @ValidateRunningAsRoot
    @patchConfigurators(
        (
            MockModuleConfigurator('a'),
            MockModuleConfigurator('b'),
        )
    )
    def testValidFlowOnExposedFuncs(self):
        configurator.validate_config(
            'validate-config',
        )
        configurator.isconfigured(
            'is-configured',
        )
        configurator.configure(
            'configure',
        )
        configurator.remove_config(
            'remove-config',
        )

    def testConfigureFiltering(self):
        class Dummy(object):
            pass
        c, args = Dummy(), Dummy()
        setattr(c, 'name', "Mock")

        for (isconfigured, isvalid, force, expected) in (
            (YES,          True,    True,  False),
            (YES,          True,    False, False),
            (YES,          False,   True,  InvalidConfig),
            (YES,          False,   False, InvalidConfig),
            (NO,           True,    True,  True),
            (NO,           True,    False, True),
            (NO,           False,   True,  True),
            (NO,           False,   False, True),
            (MAYBE,        True,    True,  True),
            (MAYBE,        True,    False, False),
            (MAYBE,        False,   True,  True),
            (MAYBE,        False,   False, InvalidConfig),
        ):
            setattr(c, 'isconfigured', lambda: isconfigured)
            setattr(c, 'validate', lambda: isvalid)
            if isinstance(expected, bool):
                self.assertEquals(
                    configurator._should_configure(c, force),
                    expected
                )
            else:
                self.assertRaises(
                    InvalidConfig,
                    configurator._should_configure, c, force
                )


class LibvirtModuleConfigureTests(TestCase):

    test_env = {}

    srcPath = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    def setUp(self):
        self._test_dir = tempfile.mkdtemp()

        self.test_env['VDSM_CONF'] = self._test_dir + '/vdsm.conf'
        self.test_env['LCONF'] = self._test_dir + '/libvirtd.conf'
        self.test_env['QCONF'] = self._test_dir + '/qemu.conf'
        self.test_env['LDCONF'] = self._test_dir + '/qemu-sanlock.conf'
        self.test_env['QLCONF'] = self._test_dir + '/libvirtd'
        self.test_env['LRCONF'] = self._test_dir + '/logrotate-libvirtd'
        self.test_env['QNETWORK'] = 'NON_EXISTENT'
        self.test_env['LRCONF_EXAMPLE'] = os.path.join(
            LibvirtModuleConfigureTests.srcPath,
            'lib/vdsm/tool/libvirtd.logrotate'
        )

        for key, val in self.test_env.items():
            libvirt.FILES[key]['path'] = val

        self._setConfig(
            ('QLCONF', 'libvirtd'),
            ('LDCONF', 'qemu_sanlock'),
        )

        self.patch = monkeypatch.Patch([
            (
                os,
                'getuid',
                lambda: 0
            ),
            (
                libvirt,
                '_getFile',
                lambda x: self.test_env[x]
            ),
            (
                libvirt,
                '_sysvToUpstart',
                lambda: True
            ),
            (
                utils,
                'isOvirtNode',
                lambda: False
            )
        ])

        self.patch.apply()

    def tearDown(self):
        self.patch.revert()
        utils.rmTree(self._test_dir)

    # helpers
    def _setConfig(self, *configurations):
        for file_, type_ in configurations:
            with open(os.path.join(dirName,
                                   'toolTests_%s.conf' % type_)) as template:
                data = template.read()
                data = data % {
                    'LATEST_CONF_VERSION': libvirt.CONF_VERSION}
            with open(self.test_env[file_], 'w') as testConf:
                testConf.write(data)

    def testValidatePositive(self):
        self._setConfig(
            ('VDSM_CONF', 'vdsm_ssl'),
            ('LCONF', 'lconf_ssl'),
            ('QCONF', 'qemu_ssl'),
        )

        self.assertTrue(libvirt.validate())

    def testValidateNegative(self):
        self._setConfig(
            ('VDSM_CONF', 'vdsm_no_ssl'),
            ('LCONF', 'lconf_ssl'),
            ('QCONF', 'qemu_ssl'),
        )

        self.assertFalse(libvirt.validate())

    def testIsConfiguredPositive(self):
        self._setConfig(
            ('LCONF', 'lconf_ssl'),
            ('QCONF', 'qemu_ssl'),
            ('LRCONF', 'libvirt_logrotate')

        )
        self.assertEqual(
            libvirt.isconfigured(),
            MAYBE
        )

    def testIsConfiguredNegative(self):
        self._setConfig(
            ('LCONF', 'lconf_ssl'),
            ('QCONF', 'empty'),
            ('LRCONF', 'empty'),
        )
        self.assertEqual(
            libvirt.isconfigured(),
            NO
        )

    def testLibvirtConfigureToSSLTrue(self):
        self._setConfig((
            'LCONF', 'empty'),
            ('VDSM_CONF', 'vdsm_ssl'),
            ('QCONF', 'empty'),
            ('LRCONF', 'empty'),
        )

        self.assertEqual(
            libvirt.isconfigured(),
            NO
        )

        libvirt.configure()

        self.assertEqual(
            libvirt.isconfigured(),
            MAYBE
        )

    def testLibvirtConfigureToSSLFalse(self):
        self._setConfig(
            ('LCONF', 'empty'),
            ('VDSM_CONF', 'vdsm_no_ssl'),
            ('QCONF', 'empty'),
            ('LRCONF', 'empty'),
        )
        self.assertEquals(
            libvirt.isconfigured(),
            NO
        )

        libvirt.configure()

        self.assertEqual(
            libvirt.isconfigured(),
            MAYBE
        )


class ConfigFileTests(TestCase):
    def setUp(self):
        fd, self.tname = tempfile.mkstemp()
        os.close(fd)

    def tearDown(self):
        os.remove(self.tname)

    # helper function
    def _writeConf(self, text):
        with open(self.tname, 'w') as f:
            f.write(text)

    def testAddExistingConf(self):
        self._writeConf("key1=val1\n"
                        "    key2    =val2\n"
                        "#key3=val4")
        with ConfigFile(self.tname,
                        version='3.4.4',
                        sectionStart="# start conf",
                        sectionEnd="# end conf") as conf:
            conf.addEntry("key3", "val3")
            conf.addEntry("key2", "val3")

        with open(self.tname, 'r') as f:
            self.assertEqual(f.read(), "key1=val1\n"
                                       "    key2    =val2\n"
                                       "#key3=val4"
                                       "# start conf-3.4.4\n"
                                       "key3=val3\n"
                                       "# end conf-3.4.4\n")

    def testSort(self):
        self._writeConf("")
        with ConfigFile(self.tname,
                        version='3.4.4',
                        sectionStart="# start conf",
                        sectionEnd="# end conf") as conf:
            conf.addEntry("key3", "val")
            conf.addEntry("key2", "val")
            conf.addEntry("key1", "val")
            conf.addEntry("key4", "val")

        with open(self.tname, 'r') as f:
            self.assertEqual(f.read(), "# start conf-3.4.4\n"
                                       "key1=val\n"
                                       "key2=val\n"
                                       "key3=val\n"
                                       "key4=val\n"
                                       "# end conf-3.4.4\n")

    def testPrefixAndPrepend(self):
        self._writeConf("/var/log/libvirt/libvirtd.log {\n"
                        "        weekly\n"
                        "}\n")
        with ConfigFile(self.tname,
                        version='3.4.4',
                        sectionStart="# start conf",
                        sectionEnd="# end conf",
                        prefix="# comment ") as conf:
                    conf.prefixLines()
                    conf.prependSection("Some text to\n"
                                        "add at the top\n")
        with open(self.tname, 'r') as f:
            self.assertEqual(f.read(),
                             "# start conf-3.4.4\n"
                             "Some text to\n"
                             "add at the top\n"
                             "# end conf-3.4.4\n"
                             "# comment /var/log/libvirt/libvirtd.log {\n"
                             "# comment         weekly\n"
                             "# comment }\n")

    def testPrefixIdempotencey(self):
        original = (
            "/var/log/libvirt/libvirtd.log {\n"
            "        weekly\n"
            "}\n"
        )
        self._writeConf(original)
        with ConfigFile(self.tname,
                        version='3.4.4',
                        sectionStart="# start conf",
                        sectionEnd="# end conf",
                        prefix="# comment ") as conf:
                    conf.prefixLines()
        with open(self.tname, 'r') as f:
            self.assertEqual(f.read(),
                             "# comment /var/log/libvirt/libvirtd.log {\n"
                             "# comment         weekly\n"
                             "# comment }\n")
        with ConfigFile(self.tname,
                        version='3.4.4',
                        sectionStart="# start conf",
                        sectionEnd="# end conf",
                        prefix="# comment ") as conff:
            conff.unprefixLines()
        with open(self.tname, 'r') as f:
            self.assertEqual(f.read(), original)

    def testRemoveEntireLinePrefix(self):
        self._writeConf("# comment\n")
        with ConfigFile(self.tname,
                        version='3.4.4',
                        sectionStart="# start conf",
                        sectionEnd="# end conf",
                        prefix="# comment") as conf:
            conf.unprefixLines()
        with open(self.tname, 'r') as f:
            self.assertEqual(f.read(), "\n")

    def testRemoveConfSection(self):
        self._writeConf("key=val\n"
                        "remove me!(see 'Backward compatibility')# by vdsm\n"
                        "key=val\n"
                        "# start conf-text-here don't matter\n"
                        "all you sections are belong to us\n"
                        "# end conf-text-here don't matter\n"
                        "# comment line\n")
        with ConfigFile(self.tname,
                        version='3.4.4',
                        sectionStart="# start conf",
                        sectionEnd="# end conf",
                        prefix="# comment") as conf:
                            conf.removeConf()
        with open(self.tname, 'r') as f:
                    self.assertEqual(f.read(), "key=val\n"
                                               "key=val\n"
                                               "# comment line\n")

    def testOutOfContext(self):
        conff = ConfigFile(self.tname,
                           version='3.4.4',
                           sectionStart="# start conf",
                           sectionEnd="# end conf")
        self.assertRaises(RuntimeError, conff.prefixLines)
        self.assertRaises(RuntimeError, conff.removeConf)

    def testHasConf(self):
        self._writeConf("key=val\n"
                        "kay=val\n"
                        "# start conf-3.4.4\n"
                        "all you sections are belong to us\n"
                        "# end conf-3.4.4\n")
        self.assertTrue(ConfigFile(self.tname,
                                   version='3.4.4',
                                   sectionStart="# start conf",
                                   sectionEnd="# end conf").hasConf())

    def testConfRead(self):
        self._writeConf("key=val\n"
                        "key1=val1\n")
        conff = ParserWrapper(None)
        conff.read(self.tname)
        self.assertEqual(conff.get('key'), 'val')

    def testConfDefaults(self):
        self._writeConf("key=val\n"
                        "key1=val1\n")
        conff = ParserWrapper({'key2': 'val2'})
        conff.read(self.tname)
        self.assertEqual(conff.get('key2'), 'val2')


class UpgradeTests(TestCase):

    class UpgraduratorTM(object):
        name = 'test'

        def __init__(self):
            self.invocations = 0

        def extendArgParser(self, ap):
            ap.add_argument('--foo',
                            dest='foo',
                            default=False,
                            action='store_true')

        def run(self, ns, args):
            self.invocations += 1
            self.ns = ns
            self.args = args
            return 0

    class BadUpgraduratorTM(object):
        name = 'bad'

        def run(self, ns, args):
            raise RuntimeError()

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.patch = monkeypatch.Patch([(upgrade, 'P_VDSM_LIB',
                                         self.temp_dir)])
        os.mkdir(os.path.join(self.temp_dir, 'upgrade'))
        self.patch.apply()

    def tearDown(self):
        self.patch.revert()
        shutil.rmtree(self.temp_dir)

    def _checkSealExists(self, name):
        return os.path.exists(os.path.join(self.temp_dir, 'upgrade', name))

    def assertSealed(self, name):
        self.assertTrue(self._checkSealExists(name))

    def assertNotSealed(self, name):
        self.assertFalse(self._checkSealExists(name))

    def testRunOnce(self):
        upgrade_obj = self.UpgraduratorTM()
        ret = upgrade.apply_upgrade(upgrade_obj, 'test')
        self.assertEquals(ret, 0)
        self.assertEquals(upgrade_obj.invocations, 1)
        self.assertSealed('test')

    def testErrorInUpgrade(self):
        bad = self.BadUpgraduratorTM()
        ret = upgrade.apply_upgrade(bad, 'foobar')
        self.assertEquals(ret, 1)
        self.assertNotSealed('bad')

    def testRunMany(self):
        upgrade_obj = self.UpgraduratorTM()
        for _ in range(5):
            upgrade.apply_upgrade(upgrade_obj, 'test')
        self.assertEquals(upgrade_obj.invocations, 1)
        self.assertSealed('test')

    def testRunAgain(self):
        upgrade_obj = self.UpgraduratorTM()
        self.assertNotSealed('test')
        upgrade.apply_upgrade(upgrade_obj, 'test')
        self.assertSealed('test')
        self.assertEquals(upgrade_obj.invocations, 1)
        upgrade.apply_upgrade(upgrade_obj, 'test')
        self.assertEquals(upgrade_obj.invocations, 1)
        upgrade.apply_upgrade(upgrade_obj, 'test', '--run-again')
        self.assertEquals(upgrade_obj.invocations, 2)
        upgrade.apply_upgrade(upgrade_obj, 'test')
        self.assertEquals(upgrade_obj.invocations, 2)
        self.assertSealed('test')

    def testUpgradeArgs(self):
        upgrade_obj = self.UpgraduratorTM()
        upgrade.apply_upgrade(upgrade_obj, 'test', '1', '2', '3')
        self.assertEquals(upgrade_obj.args, ['1', '2', '3'])

    def testParams(self):
        upgrade_obj = self.UpgraduratorTM()
        upgrade.apply_upgrade(upgrade_obj, 'test', '--foo')
        self.assertTrue(upgrade_obj.ns.foo)

        upgrade_obj.name = 'test_again'
        upgrade.apply_upgrade(upgrade_obj, 'test')
        self.assertFalse(upgrade_obj.ns.foo)
        self.assertSealed('test_again')
