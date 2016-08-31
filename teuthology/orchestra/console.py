import logging
import os
import pexpect
import subprocess
import time

from teuthology import lockstatus as ls
from teuthology.config import config

from ..exceptions import ConsoleError

import remote

try:
    import libvirt
except ImportError:
    libvirt = None

log = logging.getLogger(__name__)


class PhysicalConsole():
    """
    Physical Console (set from getRemoteConsole)
    """
    def __init__(self, name, ipmiuser, ipmipass, ipmidomain, logfile=None,
                 timeout=20):
        self.name = name
        self.shortname = remote.getShortName(name)
        self.timeout = timeout
        self.logfile = None
        self.ipmiuser = ipmiuser or config.ipmi_user
        self.ipmipass = ipmipass or config.ipmi_password
        self.ipmidomain = ipmidomain or config.ipmi_domain
        self.has_ipmi_credentials = all(map(
            lambda x: x is not None,
            [self.ipmiuser, self.ipmipass, self.ipmidomain]
        ))

    def _check_credentials(self):
        if not self.has_ipmi_credentials:
            log.error(
                "Must set ipmi_user, ipmi_password, and ipmi_domain in " \
                ".teuthology.yaml"
            )

    def _pexpect_spawn(self, cmd):
        """
        Run the cmd specified using ipmitool.
        """
        self._check_credentials()
        full_command = self._build_command(cmd)
        log.debug('pexpect command: %s', full_command)
        child = pexpect.spawn(
            full_command,
            logfile=self.logfile,
        )
        return child

    def _build_command(self, subcommand):
        template = \
            'ipmitool -H {s}.{dn} -I lanplus -U {ipmiuser} -P {ipmipass} {cmd}'
        return template.format(
            cmd=subcommand,
            s=self.shortname,
            dn=self.ipmidomain,
            ipmiuser=self.ipmiuser,
            ipmipass=self.ipmipass,
        )

    def _exit_session(self, child, timeout=None):
        child.send('~.')
        t = timeout or self.timeout
        r = child.expect(
            ['terminated ipmitool', pexpect.TIMEOUT, pexpect.EOF], timeout=t)
        if r != 0:
            self._pexpect_spawn('sol deactivate')

    def _wait_for_login(self, timeout=None, attempts=2):
        """
        Wait for login.  Retry if timeouts occur on commands.
        """
        t = timeout or self.timeout
        log.debug('Waiting for login prompt on {s}'.format(s=self.shortname))
        # wait for login prompt to indicate boot completed
        for i in range(0, attempts):
            start = time.time()
            while time.time() - start < t:
                child = self._pexpect_spawn('sol activate')
                child.send('\n')
                log.debug('expect: {s} login'.format(s=self.shortname))
                r = child.expect(
                    ['{s} login: '.format(s=self.shortname),
                     pexpect.TIMEOUT,
                     pexpect.EOF],
                    timeout=(t - (time.time() - start)))
                log.debug('expect before: {b}'.format(b=child.before))
                log.debug('expect after: {a}'.format(a=child.after))

                self._exit_session(child)
                if r == 0:
                    return
        raise ConsoleError("Did not get a login prompt from %s!" % self.name)

    def check_power(self, state, timeout=None):
        """
        Check power.  Retry if EOF encountered on power check read.
        """
        timeout = timeout or self.timeout
        t = 1
        total = t
        ta = time.time()
        while total < timeout:
            c = self._pexpect_spawn('power status')
            r = c.expect(['Chassis Power is {s}'.format(
                s=state), pexpect.EOF, pexpect.TIMEOUT], timeout=t)
            tb = time.time()
            if r == 0:
                return True
            elif r == 1:
                # keep trying if EOF is reached, first sleep for remaining
                # timeout interval
                if tb - ta < t:
                    time.sleep(t - (tb - ta))
            # go around again if EOF or TIMEOUT
            ta = tb
            t *= 2
            total += t
        return False

    def check_status(self, timeout=None):
        """
        Check status.  Returns True if console is at login prompt
        """
        try:
            # check for login prompt at console
            self._wait_for_login(timeout)
            return True
        except Exception as e:
            log.info('Failed to get ipmi console status for {s}: {e}'.format(
                s=self.shortname, e=e))
            return False

    def power_cycle(self):
        """
        Power cycle and wait for login.
        """
        log.info('Power cycling {s}'.format(s=self.shortname))
        child = self._pexpect_spawn('power cycle')
        child.expect('Chassis Power Control: Cycle', timeout=self.timeout)
        self._wait_for_login(timeout=300)
        log.info('Power cycle for {s} completed'.format(s=self.shortname))

    def hard_reset(self):
        """
        Perform physical hard reset.  Retry if EOF returned from read
        and wait for login when complete.
        """
        log.info('Performing hard reset of {s}'.format(s=self.shortname))
        start = time.time()
        while time.time() - start < self.timeout:
            child = self._pexpect_spawn('power reset')
            r = child.expect(['Chassis Power Control: Reset', pexpect.EOF],
                             timeout=self.timeout)
            if r == 0:
                break
        self._wait_for_login()
        log.info('Hard reset for {s} completed'.format(s=self.shortname))

    def power_on(self):
        """
        Physical power on.  Loop checking cmd return.
        """
        log.info('Power on {s}'.format(s=self.shortname))
        start = time.time()
        while time.time() - start < self.timeout:
            child = self._pexpect_spawn('power on')
            r = child.expect(['Chassis Power Control: Up/On', pexpect.EOF],
                             timeout=self.timeout)
            if r == 0:
                break
        if not self.check_power('on'):
            log.error('Failed to power on {s}'.format(s=self.shortname))
        log.info('Power on for {s} completed'.format(s=self.shortname))

    def power_off(self):
        """
        Physical power off.  Loop checking cmd return.
        """
        log.info('Power off {s}'.format(s=self.shortname))
        start = time.time()
        while time.time() - start < self.timeout:
            child = self._pexpect_spawn('power off')
            r = child.expect(['Chassis Power Control: Down/Off', pexpect.EOF],
                             timeout=self.timeout)
            if r == 0:
                break
        if not self.check_power('off', 60):
            log.error('Failed to power off {s}'.format(s=self.shortname))
        log.info('Power off for {s} completed'.format(s=self.shortname))

    def power_off_for_interval(self, interval=30):
        """
        Physical power off for an interval. Wait for login when complete.

        :param interval: Length of power-off period.
        """
        log.info('Power off {s} for {i} seconds'.format(
            s=self.shortname, i=interval))
        child = self._pexpect_spawn('power off')
        child.expect('Chassis Power Control: Down/Off', timeout=self.timeout)

        time.sleep(interval)

        child = self._pexpect_spawn('power on')
        child.expect('Chassis Power Control: Up/On', timeout=self.timeout)
        self._wait_for_login()
        log.info('Power off for {i} seconds completed'.format(
            s=self.shortname, i=interval))

    def spawn_sol_log(self, dest_path):
        """
        Using the subprocess module, spawn an ipmitool process using 'sol
        activate' and redirect its output to a file.

        :returns: a subprocess.Popen object
        """
        self._check_credentials()
        ipmi_cmd = self._build_command('sol activate')
        pexpect_templ = \
            "import pexpect; " \
            "pexpect.run('{cmd}', logfile=file('{log}', 'w'), timeout=None)"
        python_cmd = 'python -c "%s"' % pexpect_templ.format(
            cmd=ipmi_cmd,
            log=dest_path,
        )
        proc = subprocess.Popen(
            python_cmd,
            shell=True,
            env=os.environ,
        )
        return proc


class VirtualConsole():
    """
    Virtual Console (set from getRemoteConsole)
    """
    def __init__(self, name):
        if libvirt is None:
            raise RuntimeError("libvirt not found")

        self.shortname = remote.getShortName(name)
        status_info = ls.get_status(self.shortname)
        try:
            if status_info.get('is_vm', False):
                phys_host = status_info['vm_host']['name'].split('.')[0]
        except TypeError:
            return
        self.connection = libvirt.open(phys_host)
        for i in self.connection.listDomainsID():
            d = self.connection.lookupByID(i)
            if d.name() == self.shortname:
                self.vm_domain = d
                break
        return

    def check_power(self, state, timeout=None):
        """
        Return true if vm domain state indicates power is on.
        """
        return self.vm_domain.info[0] in [libvirt.VIR_DOMAIN_RUNNING,
                                          libvirt.VIR_DOMAIN_BLOCKED,
                                          libvirt.VIR_DOMAIN_PAUSED]

    def check_status(self, timeout=None):
        """
        Return true if running.
        """
        return self.vm_domain.info()[0] == libvirt.VIR_DOMAIN_RUNNING

    def power_cycle(self):
        """
        Simiulate virtual machine power cycle
        """
        self.vm_domain.info().destroy()
        self.vm_domain.info().create()

    def hard_reset(self):
        """
        Simiulate hard reset
        """
        self.vm_domain.info().destroy()

    def power_on(self):
        """
        Simiulate power on
        """
        self.vm_domain.info().create()

    def power_off(self):
        """
        Simiulate power off
        """
        self.vm_domain.info().destroy()

    def power_off_for_interval(self, interval=30):
        """
        Simiulate power off for an interval.
        """
        log.info('Power off {s} for {i} seconds'.format(
            s=self.shortname, i=interval))
        self.vm_domain.info().destroy()
        time.sleep(interval)
        self.vm_domain.info().create()
        log.info('Power off for {i} seconds completed'.format(
            s=self.shortname, i=interval))