#!/usr/bin/env python
"""
rna_installer.py
Part of Rail-RNA

Contains a class for installing Rail-RNA.
"""
import sys
import contextlib
import os
base_path = os.path.abspath(os.path.dirname(os.path.realpath(__file__)))
utils_path = os.path.join(base_path, 'rna', 'utils')
import site
site.addsitedir(base_path)
site.addsitedir(utils_path)
import dependency_urls
from distutils.util import strtobool
from dooplicity.tools import which, register_cleanup
import zipfile
import shutil
import subprocess
from version import version_number
import multiprocessing
import tempfile
from tempdel import remove_temporary_directories
from rna_config import print_to_screen
import glob

@contextlib.contextmanager
def cd(dir_name):
    """ Changes directory in a context only. Borrowed from AWS CLI code. """
    original_dir = os.getcwd()
    os.chdir(dir_name)
    try:
        yield
    finally:
        os.chdir(original_dir)

class RailRnaInstaller(object):
    """ Installs Rail-RNA and its assorted dependencies.

        Init vars
        -------------
        archive_name: path to (currently executing) zip containing Rail-RNA
    """

    def __init__(self, zip_name, curl_exe=None, install_dir=None,
                    no_dependencies=False):
        print_to_screen(u"""{0} Rail-RNA v{1} Installer""".format(
                                        u'\u2200', version_number)
                                    )
        if sys.platform in ['linux', 'linux2']:
            self.depends = dependency_urls.linux_dependencies
        elif sys.platform == 'darwin':
            self.depends = dependency_urls.mac_dependencies
        else:
            print_to_screen(
                    'Rail-RNA cannot be installed because it is not supported '
                    'by your OS. Currently supported OSes are Mac OS X and '
                    'Linux.'
                )
            sys.exit(1)
        self.install_dir = install_dir
        self.no_dependencies = no_dependencies
        self.zip_name = os.path.abspath(zip_name)
        self.curl_exe = curl_exe
        log_dir = tempfile.mkdtemp()
        self.log_file = os.path.join(log_dir, 'rail-rna_install.log')
        self.log_stream = open(self.log_file, 'w')
        self.finished = False
        register_cleanup(remove_temporary_directories, [log_dir])

    def __enter__(self):
        return self

    def _print_to_screen_and_log(self, message, **kwargs):
        print >>self.log_stream, message
        print_to_screen(message, **kwargs)

    def _bail(self):
        """ Copy log to some temporary dir and GTFO. """
        new_log_file = os.path.join(tempfile.mkdtemp(),
                                            'rail-rna_installer.log')
        shutil.copyfile(self.log_file, new_log_file)
        print_to_screen('Installation log may be found at %s.' % new_log_file)
        sys.exit(1)

    def _yes_no_query(self, question):
        """ Gets a yes/no answer from the user.

            question: string with question to be printed to console

            Return value: boolean
        """
        while True:
            sys.stdout.write('%s [y/n]: ' % question)
            try:
                try:
                    return strtobool(raw_input().lower())
                except KeyboardInterrupt:
                    sys.stdout.write('\n')
                    sys.exit(0)
            except ValueError:
                sys.stdout.write('Please enter \'y\' or \'n\'.\n')

    def _grab_and_explode(self, url, name):
        """ Special method for grabbing and exploding a package, if necessary.

            Does not verify URL since these are preverified. Package is
            downloaded to current directory.

            url: url to grab
            name: name of download

            No return value
        """
        self._print_to_screen_and_log('[Installing] Downloading %s...' % name,
                                        newline=False,
                                        carriage_return=True)
        command = [self.curl_exe, '-L', '-O', url]
        filename = url.rpartition('/')[2]
        try:
            subprocess.check_output(command, stderr=self.log_stream)
        except subprocess.CalledProcessError as e:
            self._print_to_screen_and_log(
                    ('Error encountered downloading file %s; exit '
                     'code was %d; command invoked was "%s".') %
                        (url, e.returncode, ' '.join(command))
                )
            self._print_to_screen_and_log('Make sure web access is available.')
            self._bail()
        else:
            # Explode
            explode_command = None
            if url[-8:] == '.tar.bz2':
                explode_command = ['tar', 'xvjf', filename]
            elif url[-7:] == '.tar.gz' or url[-4:] == '.tgz':
                explode_command = ['tar', 'xvjf', filename]
            elif url[-4:] == '.zip':
                self._print_to_screen_and_log(
                        '[Installing] Extracting %s...' % name,
                        newline=False,
                        carriage_return=True)
                try:
                    with zipfile.ZipFile(filename) as zip_object:
                        zip_object.extractall()
                except Exception as e:
                    self._print_to_screen_and_log(
                            'Error encountered exploding %s.'
                                % filename
                        )
                    self._bail()
                finally:
                    os.remove(filename)
            if explode_command is not None:
                self._print_to_screen_and_log(
                        '[Installing] Extracting %s...' % name,
                        newline=False,
                        carriage_return=True)
                try:
                    subprocess.check_output(explode_command,
                                            stderr=self.log_stream)
                except subprocess.CalledProcessError as e:
                    self._print_to_screen_and_log(
                        ('Error encountered exploding file %s; exit '
                         'code was %d; command invoked was "%s".') %
                            (filename, e.returncode, ' '.join(explode_command))
                    )
                    self._bail()
                finally:
                    os.remove(filename)

    def install(self):
        """ Installs Rail-RNA and all its dependencies. """
        if not self.no_dependencies and self.curl_exe is None:
            self.curl_exe = which('curl')
            if self.curl_exe is None:
                print_to_screen('Rail-RNA\'s installer requires Curl if '
                                'dependencies are to be installed. '
                                'Download it at '
                                'http://curl.haxx.se/download.html and use '
                                '--curl to specify its path, or '
                                'disable installing dependencies with '
                                '--no-dependencies.')
                sys.exit(1)
        if self._yes_no_query(
                'Rail-RNA can be installed for all users or just the '
                'current user.\n    * Install for all users?'
            ):
            if os.getuid():
                print_to_screen('Rerun with sudo privileges to install '
                                'for all users.')
                sys.exit(0)
            install_dir = '/usr/local'
            self.local = False
        else:
            install_dir = os.path.abspath(os.path.expanduser('~/'))
            self.local = True
        bin_dir = os.path.join(install_dir, 'bin')
        rail_exe = os.path.join(bin_dir, 'rail-rna')
        if self.install_dir is None:
            self.final_install_dir = os.path.join(install_dir, 'rail-rna')
        else:
            # User specified an installation directory
            self.final_install_dir = self.install_dir
        # Install in a temporary directory first, then move to final dest
        temp_install_dir = tempfile.mkdtemp()
        register_cleanup(remove_temporary_directories, [temp_install_dir])
        if os.path.exists(self.final_install_dir):
            if self._yes_no_query(
                    ('The installation path {dir} already exists.\n    '
                    '* Overwrite {dir}?').format(dir=self.final_install_dir)
                ):
                try:
                    shutil.rmtree(self.final_install_dir)
                except OSError:
                    # Handle this later if directory creation fails
                    pass
                try:
                    os.remove(self.final_install_dir)
                except OSError:
                    pass
            else:
                print_to_screen(
                        'Specify a different installation directory with '
                        '--install-dir.'
                    )
                sys.exit(0)
        self._print_to_screen_and_log('[Installing] Extracting Rail-RNA...',
                                        newline=False,
                                        carriage_return=True)
        try:
            os.makedirs(self.final_install_dir)
        except OSError as e:
            self._print_to_screen_and_log(
                            ('Problem encountered trying to create '
                             'directory %s for installation. May need '
                             'sudo permissions.') % self.final_install_dir
                        )
            self._bail()
        else:
            # So it's possible to move temp installation dir there
            os.rmdir(self.final_install_dir)
            pass
        if not self.no_dependencies:
            with cd(temp_install_dir):
                with zipfile.ZipFile(self.zip_name) as zip_object:
                    zip_object.extractall()
                self._grab_and_explode(self.depends['bowtie1'], 'Bowtie 1')
                self._grab_and_explode(self.depends['bowtie2'], 'Bowtie 2')
                self._grab_and_explode(self.depends['bedgraphtobigwig'],
                                        'BedGraphToBigWig')
                self._grab_and_explode(self.depends['pypy'], 'PyPy')
                self._grab_and_explode(self.depends['samtools'], 'SAMTools')
            # Have to make SAMTools (annoying; maybe change this)
            samtools_dir = os.path.join(temp_install_dir,
                    self.depends['samtools'].rpartition('/')[2][:-8]
                )
            with cd(samtools_dir):
                # Make on all but one cylinder
                thread_count = max(1, multiprocessing.cpu_count() - 1)
                samtools_command = ['make', '-j%d' % thread_count]
                self._print_to_screen_and_log(
                            '[Installing] Making SAMTools...',
                            newline=False,
                            carriage_return=True
                        )
                try:
                    subprocess.check_output(samtools_command,
                                                stderr=self.log_stream)
                except subprocess.CalledProcessError as e:
                    self._print_to_screen_and_log(
                            ('Error encountered making SAMTools; exit '
                             'code was %d; command invoked was "%s".') %
                                (e.returncode, ' '.join(samtools_command))
                        )
                    self._bail()
            samtools = os.path.join(self.final_install_dir,
                            self.depends['samtools'].rpartition('/')[2][:-8],
                            'samtools')
            bowtie1_base = '-'.join(
                    self.depends['bowtie1'].rpartition('/')[2].split('-')[:2]
                )
            bowtie1 = os.path.join(self.final_install_dir, bowtie1_base,
                                    'bowtie')
            bowtie1_build = os.path.join(self.final_install_dir, bowtie1_base,
                                            'bowtie-build')
            bowtie2_base = '-'.join(
                    self.depends['bowtie2'].rpartition('/')[2].split('-')[:2]
                )
            bowtie2 = os.path.join(self.final_install_dir, bowtie2_base,
                                    'bowtie2')
            bowtie2_build = os.path.join(self.final_install_dir, bowtie2_base,
                                            'bowtie2-build')
            pypy = os.path.join(self.final_install_dir,
                    self.depends['pypy'].rpartition('/')[2][:-8], 'bin', 'pypy'
                )
            bedgraphtobigwig = os.path.join(self.final_install_dir,
                                                'bedGraphToBigWig')
            # Write paths to exe_paths
            with open(
                            os.path.join(temp_install_dir, 'exe_paths.py'), 'w'
                        ) as exe_paths_stream:
                print >>exe_paths_stream, (
"""\"""
exe_paths.py
Part of Rail-RNA

Defines default paths of Rail-RNA's executable dependencies. Set a given
variable equal to None if the default path should be in PATH.
\"""

pypy = '{pypy}'
aws = None
curl = None
sort = None
bowtie1 = '{bowtie1}'
bowtie1_build = '{bowtie1_build}'
bowtie2 = '{bowtie2}'
bowtie2_build = '{bowtie2_build}'
samtools = '{samtools}'
bedgraphtobigwig = '{bedgraphtobigwig}'
"""
                ).format(pypy=pypy, bowtie1=bowtie1,
                            bowtie1_build=bowtie1_build, bowtie2=bowtie2,
                            bowtie2_build=bowtie2_build, samtools=samtools,
                            bedgraphtobigwig=bedgraphtobigwig)
        # Move to final directory
        try:
            os.renames(temp_install_dir, self.final_install_dir)
        except OSError:
            self._print_to_screen_and_log(('Problem encountered moving '
                                           'temporary installation %s to '
                                           'final destination %s.') % (
                                                temp_install_dir,
                                                self.final_install_dir
                                            ))
            self._bail()
        # Create shell-script executable
        try:
            os.makedirs(bin_dir)
        except OSError:
            if not os.path.isdir(bin_dir):
                self._print_to_screen_and_log(('Problem encountered creating '
                                               'directory %s.') % bin_dir
                                            )
                self._bail()
        with open(rail_exe, 'w') as rail_exe_stream:
            print >>rail_exe_stream, (
"""#!/usr/bin/env bash

{python_executable} {install_dir} $@
"""
                ).format(python_executable=sys.executable,
                            install_dir=self.final_install_dir)
        if self.local:
            '''Have to add Rail to PATH. Do this in bashrc and bash_profile
            contingent on whether it's present already because of
            inconsistent behavior across Mac OS and Linux distros.'''
            to_print = (
"""
## Rail-RNA additions
if [ -d "{bin_dir}" ] && [[ ":$PATH:" != *":{bin_dir}:"* ]]; then
    PATH="${{PATH:+"$PATH:"}}{bin_dir}"
fi
## End Rail-RNA additions
"""
                ).format(bin_dir=bin_dir)
            import mmap
            bashrc = os.path.expanduser('~/.bashrc')
            bash_profile = os.path.expanduser('~/.bash_profile')
            try:
                with open(bashrc) as bashrc_stream:
                    mmapped = mmap.mmap(bashrc_stream.fileno(), 0, 
                                            access=mmap.ACCESS_READ)
                    if mmapped.find(to_print) == -1:
                        print_to_bashrc = True
                    else:
                        print_to_bashrc = False
            except (IOError, ValueError):
                # No file
                print_to_bashrc = True
            try:
                with open(bash_profile) as bash_profile_stream:
                    mmapped = mmap.mmap(bash_profile_stream.fileno(), 0, 
                                            access=mmap.ACCESS_READ)
                    if mmapped.find(to_print) == -1:
                        print_to_bash_profile = True
                    else:
                        print_to_bash_profile = False
            except (IOError, ValueError):
                # No file
                print_to_bash_profile = True
            if print_to_bashrc:
                with open(bashrc, 'a') as bashrc_stream:
                    print >>bashrc_stream, to_print
            if print_to_bash_profile:
                with open(bash_profile, 'a') as bash_profile_stream:
                    print >>bash_profile_stream, to_print
        # Set 755 permissions across Rail's dirs and 644 across files
        dir_command = ['find', self.final_install_dir, '-type', 'd',
                            '-exec', 'chmod', '755', '{}', ';']
        file_command = ['find', self.final_install_dir, '-type', 'f',
                            '-exec', 'chmod', '644', '{}', ';']
        try:
            subprocess.check_output(dir_command,
                                        stderr=self.log_stream)
        except subprocess.CalledProcessError as e:
            self._print_to_screen_and_log(
                        ('Error encountered changing directory '
                         'permissions; exit code was %d; command invoked '
                         'was "%s".') %
                            (e.returncode, ' '.join(dir_command))
                    )
            self._bail()
        try:
            subprocess.check_output(file_command,
                                        stderr=self.log_stream)
        except subprocess.CalledProcessError as e:
            self._print_to_screen_and_log(
                        ('Error encountered changing file '
                         'permissions; exit code was %d; command invoked '
                         'was "%s".') %
                            (e.returncode, ' '.join(file_command))
                    )
            self._bail()
        # Go back and set 755 permissions for executables
        for program in [rail_exe, bowtie1, bowtie1_build,
                            bowtie2, bowtie2_build, samtools,
                            bedgraphtobigwig]:
            os.chmod(program, 0755)
        # Also for misc. Bowtie executables
        for program in glob.glob(os.path.join(os.path.dirname(bowtie1),
                                    'bowtie-*')):
            os.chmod(program, 0755)
        for program in glob.glob(os.path.join(os.path.dirname(bowtie2),
                                    'bowtie2-*')):
            os.chmod(program, 0755)
        self._print_to_screen_and_log('Installed Rail-RNA.')
        install_aws = (not self.no_dependencies and not which('aws'))
        if install_aws and self._yes_no_query(
                'AWS CLI is not installed but required for Rail-RNA to work '
                'in its "elastic" mode, on Amazon Elastic MapReduce.\n'
                '    * Install AWS CLI now?'
            ):
            temp_aws_install_dir = tempfile.mkdtemp()
            register_cleanup(remove_temporary_directories,
                                [temp_aws_install_dir])
            with cd(temp_aws_install_dir):
                self._grab_and_explode(self.depends['aws'], 'AWS CLI')
                if self.local:
                    # Local install
                    aws_command = ['./awscli-bundle/install', '-b',
                                    os.path.abspath(
                                            os.path.expanduser('~/bin/aws')
                                        )]
                else:
                    # All users
                    aws_command = ['./awscli-bundle/install', '-i',
                                '/usr/local/aws', '-b', '/usr/local/bin/aws']
                try:
                    subprocess.check_output(aws_command,
                                                stderr=self.log_stream)
                except subprocess.CalledProcessError as e:
                    self._print_to_screen_and_log(
                            ('Error encountered installing AWS CLI; exit '
                             'code was %d; command invoked was "%s".') %
                                (e.returncode, ' '.join(aws_command))
                        )
                    self._bail()
            print_to_screen('Configure the AWS CLI by running '
                            '"aws configure".')
        elif install_aws:
            print_to_screen('Visit http://docs.aws.amazon.com/cli/latest/'
                            'userguide/installing.html to install the '
                            'AWS CLI later.')
        self.finished = True

    def __exit__(self, type, value, traceback):
        try:
            self.log_stream.close()
        except:
            pass
        if self.finished:
            # Stuck around, so bailing did not happen; put in rail dir
            assert hasattr(self, 'final_install_dir')
            new_log_file = os.path.join(self.final_install_dir,
                                            'rail-rna_installer.log')
            shutil.copyfile(self.log_file, new_log_file)
            print_to_screen('Installation log may be found at %s.'
                                                        % new_log_file)
            if not self.local:
                print_to_screen('Start using Rail by entering "rail-rna".')
            else:
                print_to_screen('Enter "source ~/.bash_profile", then start '
                                'using Rail by entering "rail-rna".')