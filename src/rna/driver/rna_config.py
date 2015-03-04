#!/usr/bin/env python
"""
rna_config.py
Part of Rail-RNA

Contains classes that perform error checking and generate JSON configuration
output for Rail-RNA. These configurations are parsable by Dooplicity's
emr_simulator.py and emr_runner.py.

Class structure is designed so only those arguments relevant to modes/job flows
are included.

The descriptions of command-line arguments contained here assume that a
calling script has the command-line options "prep", "align", "go", "local",
and "elastic".
"""

import os
base_path = os.path.abspath(
                    os.path.dirname(os.path.dirname(os.path.dirname(
                        os.path.realpath(__file__)))
                    )
                )
utils_path = os.path.join(base_path, 'rna', 'utils')
import site
site.addsitedir(utils_path)
site.addsitedir(base_path)
import dooplicity.ansibles as ab
import tempfile
import shutil
from dooplicity.tools import path_join, is_exe, which, register_cleanup
from version import version_number
import sys
import argparse
import subprocess
import time
from traceback import format_exc
from collections import defaultdict
import random
import string
import socket

_help_set = set(['--help', '-h'])
_argv_set = set(sys.argv)

class RailParser(argparse.ArgumentParser):
    """ Accommodates Rail-RNA's subcommand structure. """
    
    def error(self, message):
        if not _help_set.intersection(_argv_set):
            print >>sys.stderr, 'error: %s' % message
            self.print_usage()
            sys.exit(2)
        self.print_usage()
        sys.exit(0)

class RailHelpFormatter(argparse.HelpFormatter):
    """ Formats help in a more condensed way.

        Overrides not-so-public argparse API, but since the Python 2.x line is
        no longer under very active development, this is probably okay.
    """

    def _get_help_string(self, action):
        help = action.help
        if '(def: ' not in action.help and not action.required \
            and not action.const:
            if action.default is not argparse.SUPPRESS:
                defaulting_nargs = [argparse.OPTIONAL, argparse.ZERO_OR_MORE]
                if action.option_strings or action.nargs in defaulting_nargs:
                    help += ' (def: %(default)s)'
        return help

    def _format_action_invocation(self, action):
        if not action.option_strings:
            metavar, = self._metavar_formatter(action, action.dest)(1)
            return metavar
        else:
            return '%s %s' % ('/'.join(action.option_strings),
                                self._format_args(action, action.dest.upper()))

def general_usage(job_flow_and_mode, required_args=''):
    """ Special Rail-RNA usage message at subcommand level.

        job_flow_and_mode: job flow and mode separated by a space

        Return value: usage message
    """
    return \
"""rail-rna {0} {1}<[opts]>{2}""".format(
job_flow_and_mode, required_args,
"""

Add --help/-h to view help.""" if not _help_set.intersection(_argv_set)
else ''
)

def rail_help_wrapper(prog):
    """ So formatter_class's max_help_position can be changed. """
    return RailHelpFormatter(prog, max_help_position=37)

'''These are placed here for convenience; their locations may change
on EMR depending on bootstraps.'''
_hadoop_streaming_jar = '/home/hadoop/contrib/streaming/hadoop-streaming.jar'
_multiple_files_jar = '/mnt/lib/multiple-files.jar'
_relevant_elephant_jar = '/mnt/lib/relevant-elephant.jar'
_hadoop_lzo_jar = ('/home/hadoop/.versions/2.4.0/share/hadoop'
                   '/common/lib/hadoop-lzo.jar')
_s3distcp_jar = '/home/hadoop/lib/emr-s3distcp-1.0.jar'
_hdfs_temp_dir = 'hdfs:///railtemp'
_base_combine_split_size = 268435456 # 250 MB
_elastic_bowtie1_idx = '/mnt/index/genome'
_elastic_bowtie2_idx = '/mnt/index/genome'
_elastic_bedgraphtobigwig_exe ='/mnt/bin/bedGraphToBigWig'
_elastic_samtools_exe = 'samtools'
_elastic_bowtie1_exe = 'bowtie'
_elastic_bowtie2_exe = 'bowtie2'
_elastic_bowtie1_build_exe = 'bowtie-build'
_elastic_bowtie2_build_exe = 'bowtie2-build'

# Decide Python executable
if 'pypy 2.' in sys.version.lower():
    # Executable has the user's desired version of PyPy
    _warning_message = 'Launching Dooplicity runner with PyPy...'
    _executable = sys.executable
else:
    _pypy_exe = which('pypy')
    _print_warning = False
    if _pypy_exe is not None:
        try:
            if 'pypy 2.' in \
                subprocess.check_output(
                        [_pypy_exe, '--version'],
                        stderr=subprocess.STDOUT
                    ).lower():
                _executable = _pypy_exe
            else:
                _executable = sys.executable
                _print_warning = True
        except Exception as e:
            _executable = sys.executable
            _print_warning = True
    else:
        _print_warning = True
    if _print_warning:
        _warning_message = ('WARNING: PyPy 2.x not found. '
            'Installation is recommended to optimize performance '
            'of Rail-RNA in "local" or "parallel" mode. If it is installed, '
            'make sure the "pypy" executable is in PATH, or use '
            'it to execute Rail-RNA via "[path to \'pypy\'] '
            '[path to \'rail-rna\']".')
        _executable = sys.executable
    else:
        _warning_message = 'Launching Dooplicity runner with PyPy...'

def print_to_screen(message, newline=True, carriage_return=False):
    """ Prints message to stdout as well as stderr if stderr is redirected.

        message: message to print
        newline: True iff newline should be printed
        carriage_return: True iff carriage return should be printed; also
            clears line with ANSI escape code

        No return value.
    """
    sys.stderr.write('\x1b[K' + message + ('\r' if carriage_return else '')
                        + ('\n' if newline else ''))
    if sys.stderr.isatty():
        sys.stderr.flush()
    else:
        # So the user sees it too
        sys.stdout.write('\x1b[K' + message + ('\r' if carriage_return else '')
                        + ('\n' if newline else ''))
        sys.stdout.flush()

def engine_string_from_list(id_list):
    """ Pretty-prints list of engine IDs.

        id_list: list of engine IDs

        Return value: string condensing list of engine IDs
    """
    id_list = sorted(set(id_list))
    to_print = []
    if not id_list: return ''
    last_id = id_list[0]
    streak = 0
    for engine_id in id_list[1:]:
        if engine_id == last_id + 1:
            streak += 1
        else:
            if streak > 1:
                to_print.append('%d-%d' % (last_id - streak, last_id))
            elif streak == 1:
                to_print.append('%d, %d' % (last_id - 1, last_id))
            else:
                to_print.append('%d' % last_id)
            streak = 0
        last_id = engine_id
    if streak > 1:
        to_print.append('%d-%d' % (last_id - streak, last_id))
    elif streak == 1:
        to_print.append('%d, %d' % (last_id - 1, last_id))
    else:
        to_print.append('%d' % last_id)
    if len(to_print) > 1:
        to_print[-1] = ' '.join(['and', to_print[-1]])
    return ', '.join(to_print)

def apply_async_with_errors(rc, ids, function_to_apply, *args, **kwargs):
    """ apply_async() that cleanly outputs engines responsible for exceptions.

        WARNING: in general, this method requires Dill for pickling.
        See https://pypi.python.org/pypi/dill
        and http://matthewrocklin.com/blog/work/2013/12/05
        /Parallelism-and-Serialization/

        rc: IPython parallel Cient object
        ids: IDs of engines where function_to_apply should be run
        function_to_apply: function to run across engines. If this is a
            dictionary whose keys are exactly the engine IDs, each engine ID's
            value is regarded as a distinct function corresponding to the key.
        *args: contains unnamed arguments of function_to_apply. If a given
            argument is a dictionary whose keys are exactly the engine IDs,
            each engine ID's value is regarded as a distinct argument
            corresponding to the key. The same goes for kwargs.
        **kwargs: includes --
            errors_to_ignore: list of exceptions to ignore, where each
               exception is a string
            message: message to append to exception raised
            and named arguments of function_to_apply
            dict_format: if True, returns engine-result key-value dictionary;
                if False, returns list of results

        Return value: list of AsyncResults, one for each engine spanned by
            direct_view
    """
    if 'dict_format' not in kwargs:
        dict_format = False
    else:
        dict_format = kwargs['dict_format']
        del kwargs['dict_format']
    if not ids:
        if dict_format:
            return {}
        else:
            return []
    if 'errors_to_ignore' not in kwargs:
        errors_to_ignore = []
    else:
        errors_to_ignore = kwargs['errors_to_ignore']
        del kwargs['errors_to_ignore']
    if 'message' not in kwargs:
        message = None
    else:
        message = kwargs['message']
        del kwargs['message']
    id_set = set(ids)
    if not (isinstance(function_to_apply, dict)
            and set(function_to_apply.keys()) == id_set):
        function_to_apply_holder = function_to_apply
        function_to_apply = {}
        for i in ids:
            function_to_apply[i] = function_to_apply_holder
    new_args = defaultdict(list)
    for arg in args:
        if (isinstance(arg, dict)
            and set(arg.keys()) == id_set):
            for i in arg:
                new_args[i].append(arg[i])
        else:
            for i in ids:
                new_args[i].append(arg)
    new_kwargs = defaultdict(dict)
    for kwarg in kwargs:
        if (isinstance(kwargs[kwarg], dict)
            and set(kwargs[kwarg].keys()) == id_set):
            for i in ids:
                new_kwargs[i][kwarg] = kwargs[kwarg][i]
        else:
            for i in ids:
                new_kwargs[i][kwarg] = kwargs[kwarg]
    asyncresults = []
    ids_not_to_return = set()
    for i in ids:
        asyncresults.append(
                rc[i].apply_async(
                    function_to_apply[i],*new_args[i],**new_kwargs[i]
                )
            )
    while any([not asyncresult.ready() for asyncresult in asyncresults]):
        time.sleep(1e-1)
    asyncexceptions = defaultdict(set)
    for asyncresult in asyncresults:
        try:
            asyncdict = asyncresult.get_dict()
        except Exception as e:
            exc_to_report = format_exc()
            proceed = False
            for error_to_ignore in errors_to_ignore:
                if error_to_ignore in exc_to_report:
                    proceed = True
                    ids_not_to_return.add(asyncresult.metadata['engine_id'])
            if not proceed:
                asyncexceptions[format_exc()].add(
                        asyncresult.metadata['engine_id']
                    )
    if asyncexceptions:
        runtimeerror_message = []
        for exc in asyncexceptions:
            runtimeerror_message.extend(
                    ['Engine(s) %s report(s) the following exception.'
                        % engine_string_from_list(
                              list(asyncexceptions[exc])
                            ),
                     exc]
                 )
        raise RuntimeError('\n'.join(runtimeerror_message
                            + ([message] if message else [])))
    # Return only those results for which there is no failure
    if not dict_format:
        return [asyncresult.get() for asyncresult in asyncresults
                    if asyncresult.metadata['engine_id']
                    not in ids_not_to_return]
    to_return = {}
    for i, asyncresult in enumerate(asyncresults):
        if asyncresult.metadata['engine_id'] not in ids_not_to_return:
            to_return[asyncresult.metadata['engine_id']] = asyncresult.get()
    return to_return

def ready_engines(rc, base, prep=False):
    """ Prepares engines for checks and copies Rail/manifest/index to nodes. 

        rc: IPython Client object
        base: instance of RailRnaErrors
        prep: True iff it's a preprocess job flow

        No return value.
    """
    try:
        import IPython
    except ImportError:
        # Should have been taken care of by a different fxn, but just in case
        raise RuntimeError(
               'IPython is required to run Rail-RNA in '
               '"parallel" mode. Visit ipython.org to '
               'download it, or simply download the Anaconda '
               'distribution of Python at '
               'https://store.continuum.io/cshop/anaconda/; it\'s '
               'easy to install and comes with IPython and '
               'several other useful packages.'
            )
    all_engines = rc.ids
    '''Clear remote namespaces.'''
    rc[:].clear()
    '''Test that intermediate directory is accessible from everywhere; create
    dir in process.'''
    try:
        os.makedirs(base.intermediate_dir)
    except OSError:
        # Hopefully exists
        pass
    # Create dud file in intermediate directory
    dud_filename = os.path.join(base.intermediate_dir, 
                            ''.join(random.choice(string.ascii_uppercase
                                    + string.digits) for _ in xrange(40))
                        )
    with open(dud_filename, 'w') as dud_stream:
        print >>dud_stream, 'DUD'
    '''Now test for existence of dud file across engines; a dud file with a
    very random name is created to ensure that the directory being searched for
    is really the one specified by the user rather than some directory that 
    only an engine could see, which with high probability is absent this
    file.'''
    try:
        dud_results = apply_async_with_errors(rc, all_engines, os.path.exists,
                                            dud_filename, dict_format=True,
                                            message=('Error(s) encountered '
                                                     'testing that '
                                                     'the log directory is '
                                                     'accessible from '
                                                     'all engines. Restart '
                                                     'IPython engines '
                                                     'and try again.')
                                        )
    finally:
        # No matter what, kill the dud
        os.remove(dud_filename)
    bad_engines = [engine for engine in dud_results if not dud_results[engine]]
    if bad_engines:
        raise RuntimeError(('Engines %s cannot access the log directory %s. '
                            'Ensure that the log directory is in a location '
                            'accessible from all engines.') % (
                                    engine_string_from_list(bad_engines),
                                    base.intermediate_dir
                                ))
    current_hostname = socket.gethostname()
    engine_to_hostnames = apply_async_with_errors(
                                rc, all_engines, socket.gethostname,
                                dict_format=True
                            )
    hostname_to_engines = defaultdict(set)
    for engine in engine_to_hostnames:
        hostname_to_engines[engine_to_hostnames[engine]].add(engine)
    '''Select engines to do "heavy lifting"; that is, they remove files copied
    to hosts on SIGINT/SIGTERM. Do it randomly (NO SEED) so if IWF occurs,
    second try will be different. IWF = intermittent weird failure, terminology 
    borrowed from a PC repair guide from the nineties that one of us (AN) wants
    to perpetuate.'''
    pids = apply_async_with_errors(rc, all_engines, os.getpid)
    # Set random seed so temp directory is reused if restarting Rail
    random.seed(str(sorted(pids)))
    engines_for_copying = [random.choice(list(engines)) 
                            for engines in hostname_to_engines.values()]
    '''Herd won't work with local engines, work around this by separating
    engines into two groups: local and remote.'''
    remote_hostnames_for_copying = list(
            set(hostname_to_engines.keys()).difference(set([current_hostname]))
        )
    local_engines_for_copying = [engine for engines in engines_for_copying
                                 if engine
                                 in hostname_to_engines[current_hostname]]
    '''Create temporary directories on selected nodes; NOT WINDOWS-COMPATIBLE;
    must be changed if porting Rail to Windows.'''
    if base.scratch is None:
        scratch_dir = '/tmp'
    else:
        scratch_dir = base.scratch
    temp_dir = os.path.join(scratch_dir, 'railrna-%s' %
                            ''.join(random.choice(string.ascii_uppercase
                                    + string.digits) for _ in xrange(12)))
    if not prep and not base.do_not_copy_index_to_nodes:
        dir_to_create = os.path.join(temp_dir, 'genome')
    else:
        dir_to_create = temp_dir
    apply_async_with_errors(rc, engines_for_copying, os.makedirs,
        dir_to_create,
        message=('Error(s) encountered creating temporary '
                 'directories for storing Rail on slave nodes. '
                 'Restart IPython engines and try again.'),
        errors_to_ignore=['OSError'])
    '''Only foolproof way to die is by process polling. See
    http://stackoverflow.com/questions/284325/
    how-to-make-child-process-die-after-parent-exits for more information.'''
    apply_async_with_errors(rc, engines_for_copying, subprocess.Popen,
        ('echo "trap \\"{{ rm -rf {temp_dir}; exit 0; }}\\" '
         'SIGHUP SIGINT SIGTERM EXIT; '
         'while [[ \$(ps -p \$\$ -o ppid=) -gt 1 ]]; do sleep 1; done & wait" '
         '>{temp_dir}/delscript.sh').format(temp_dir=temp_dir),
        shell=True,
        executable='/bin/bash',
        message=(
                'Error creating script for scheduling temporary directories '
                'on cluster nodes for deletion. Restart IPython engines '
                'and try again.'
            ))
    apply_async_with_errors(rc, engines_for_copying, subprocess.Popen,
            ['/usr/bin/env', 'bash', '%s/delscript.sh' % temp_dir],
            message=(
                'Error scheduling temporary directories on slave nodes '
                'for deletion. Restart IPython engines and try again.'
            ))
    # Compress Rail-RNA and distribute it to nodes
    compressed_rail_file = 'rail.tar.gz'
    compressed_rail_path = os.path.join(os.path.abspath(base.intermediate_dir),
                                            compressed_rail_file)
    compressed_rail_destination = os.path.join(temp_dir, compressed_rail_file)
    import tarfile
    with tarfile.open(compressed_rail_path, 'w:gz') as tar_stream:
        tar_stream.add(base_path, arcname='rail')
    try:
        import herd.herd as herd
    except ImportError:
        # Torrent distribution channel for compressed archive not available
        print_to_screen('Copying Rail-RNA to cluster nodes...',
                            newline=False, carriage_return=True)
        apply_async_with_errors(rc, engines_for_copying, shutil.copyfile,
            compressed_rail_path, compressed_rail_destination,
            message=('Error(s) encountered copying Rail to '
                     'slave nodes. Refer to the errors above -- and '
                     'especially make sure /tmp is not out of space on any '
                     'node supporting an IPython engine '
                     '-- before trying again.'),
        )
        print_to_screen('Copied Rail-RNA to cluster nodes.',
                            newline=True, carriage_return=False)
    else:
        if local_engines_for_copying:
            print_to_screen('Copying Rail-RNA to local filesystem...',
                            newline=False, carriage_return=True)
            apply_async_with_errors(rc, local_engines_for_copying,
                shutil.copyfile, compressed_rail_path,
                compressed_rail_destination,
                message=('Error(s) encountered copying Rail to '
                         'local filesystem. Refer to the errors above -- and '
                         'especially make sure /tmp is not out of space on '
                         'any node supporting an IPython engine '
                         '-- before trying again.'),
            )
            print_to_screen('Copied Rail-RNA to local filesystem.',
                                newline=True, carriage_return=False)
        if remote_hostnames_for_copying:
            print_to_screen('Copying Rail-RNA to remote nodes with Herd...')
            herd.run_with_opts(
                    compressed_rail_path,
                    compressed_rail_destination,
                    hostlist=','.join(remote_hostnames_for_copying)
                )
            print_to_screen('Copied Rail-RNA to remote nodes with Herd.',
                                newline=True, carriage_return=False)
    # Extract Rail
    print_to_screen('Extracting Rail-RNA on cluster nodes...',
                            newline=False, carriage_return=True)
    apply_async_with_errors(rc, engines_for_copying, subprocess.Popen,
            'tar xzf {} -C {}'.format(compressed_rail_destination, temp_dir),
            shell=True)
    print_to_screen('Extracted Rail-RNA on cluster nodes.',
                            newline=True, carriage_return=False)
    # Add Rail to path on every engine
    temp_base_path = os.path.join(temp_dir, 'rail')
    temp_utils_path = os.path.join(temp_base_path, 'rna', 'utils')
    temp_driver_path = os.path.join(temp_base_path, 'rna', 'driver')
    apply_async_with_errors(rc, all_engines, site.addsitedir, temp_base_path)
    apply_async_with_errors(rc, all_engines, site.addsitedir, temp_utils_path)
    apply_async_with_errors(rc, all_engines, site.addsitedir, temp_driver_path)
    # Copy manifest to nodes
    manifest_destination = os.path.join(temp_dir, 'MANIFEST')
    try:
        import herd.herd as herd
    except ImportError:
        print_to_screen('Copying file manifest to cluster nodes...',
                            newline=False, carriage_return=True)
        apply_async_with_errors(rc, engines_for_copying, shutil.copyfile,
            base.manifest, manifest_destination,
            message=('Error(s) encountered copying manifest to '
                     'slave nodes. Refer to the errors above -- and '
                     'especially make sure /tmp is not out of space on any '
                     'node supporting an IPython engine '
                     '-- before trying again.'),
        )
        print_to_screen('Copied file manifest to cluster nodes.',
                            newline=True, carriage_return=False)
    else:
        if local_engines_for_copying:
            print_to_screen('Copying manifest to local filesystem...',
                            newline=False, carriage_return=True)
            apply_async_with_errors(rc, local_engines_for_copying,
                shutil.copyfile, base.manifest, manifest_destination,
                message=('Error(s) encountered copying manifest to '
                         'slave nodes. Refer to the errors above -- and '
                         'especially make sure /tmp is not out of space on '
                         'any node supporting an IPython engine '
                         '-- before trying again.'),
            )
            print_to_screen('Copied manifest to local filesystem.',
                                newline=True, carriage_return=False)
        if remote_hostnames_for_copying:
            print_to_screen('Copying manifest to remote nodes with Herd...')
            herd.run_with_opts(
                    base.manifest,
                    manifest_destination,
                    hostlist=','.join(remote_hostnames_for_copying)
                )
            print_to_screen('Copied manifest to remote nodes with Herd.',
                                newline=True, carriage_return=False)
    base.old_manifest = base.manifest
    base.manifest = manifest_destination
    if not prep and not base.do_not_copy_index_to_nodes:
        index_files = ([base.bowtie2_idx + extension
                        for extension in ['.1.bt2', '.2.bt2',
                                          '.3.bt2', '.4.bt2', 
                                          '.rev.1.bt2', '.rev.2.bt2']]
                        + [base.bowtie1_idx + extension
                            for extension in [
                                    '.1.ebwt', '.2.ebwt', '.3.ebwt',
                                    '.4.ebwt', '.rev.1.ebwt', '.rev.2.ebwt'
                                ]])
        try:
            import herd.herd as herd
        except ImportError:
            print_to_screen('Warning: Herd is not installed, so copying '
                            'Bowtie indexes to cluster nodes may be slow. '
                            'Install Herd to enable torrent distribution of '
                            'indexes across nodes, or invoke '
                            '--do-not-copy-index-to-nodes to avoid copying '
                            'indexes, which may then slow down alignment.',
                            newline=True, carriage_return=False)
            files_copied = 0
            print_to_screen(
                    'Copying Bowtie index files to cluster nodes '
                    '(%d/%d files copied)...'
                    % (files_copied, len(index_files)),
                    newline=False, carriage_return=True
                )
            for index_file in index_files:
                apply_async_with_errors(rc, engines_for_copying,
                    shutil.copyfile, os.path.abspath(index_file),
                    os.path.join(temp_dir, 'genome',
                                    os.path.basename(index_file)),
                    message=('Error(s) encountered copying Bowtie indexes to '
                             'cluster nodes. Refer to the errors above -- and '
                             'especially make sure /tmp is not out of space '
                             'on any node supporting an IPython engine '
                             '-- before trying again.')
                )
                files_copied += 1
                print_to_screen(
                    'Copying Bowtie index files to cluster nodes '
                    '(%d/%d files copied)...'
                    % (files_copied, len(index_files)),
                    newline=False, carriage_return=True
                )
            print_to_screen('Copied Bowtie indexes to cluster nodes.',
                                newline=True, carriage_return=False)
        else:
            if local_engines_for_copying:
                files_copied = 0
                print_to_screen('Copying Bowtie indexes to local '
                                'filesystem (%d/%d files copied)...'
                                % (files_copied, len(index_files)),
                                newline=False, carriage_return=True)
                for index_file in index_files:
                    apply_async_with_errors(rc, engines_for_copying,
                        shutil.copyfile, os.path.abspath(index_file),
                        os.path.join(temp_dir, 'genome',
                                        os.path.basename(index_file)),
                        message=('Error(s) encountered copying Bowtie '
                                 'indexes to local filesystem. Refer to the '
                                 'errors above -- and especially make sure '
                                 '/tmp is not out of space '
                                 'on any node supporting an IPython engine '
                                 '-- before trying again.')
                    )
                    files_copied += 1
                    print_to_screen(
                        'Copying Bowtie indexes to local filesystem '
                        '(%d/%d files copied)...'
                        % (files_copied, len(index_files)),
                        newline=False, carriage_return=True
                    )
                print_to_screen('Copied Bowtie indexes to local '
                                'filesystem.',
                                newline=True, carriage_return=False)
            if remote_hostnames_for_copying:
                print_to_screen('Copying Bowtie indexes to cluster nodes '
                                'with Herd...',
                                newline=False, carriage_return=True)
                for index_file in index_files:
                    herd.run_with_options(
                            os.path.abspath(index_file),
                            os.path.join(temp_dir, 'genome',
                                os.path.basename(index_file)),
                            hostlist=','.join(hostname_to_engines.keys())
                        )
                print_to_screen('Copied Bowtie indexes to cluster nodes '
                                'with Herd.',
                                newline=True, carriage_return=False)
        base.bowtie1_idx = os.path.join(temp_dir, 'genome',
                                        os.path.basename(base.bowtie1_idx))
        base.bowtie2_idx = os.path.join(temp_dir, 'genome',
                                        os.path.basename(base.bowtie2_idx))

def step(name, inputs, output,
    mapper='org.apache.hadoop.mapred.lib.IdentityMapper',
    reducer='org.apache.hadoop.mapred.lib.IdentityReducer', 
    action_on_failure='TERMINATE_JOB_FLOW', jar=_hadoop_streaming_jar,
    tasks=0, partition_field_count=None, key_fields=None, archives=None,
    multiple_outputs=False, inputformat=None, extra_args=[]):
    """ Outputs JSON for a given step.

        name: name of step
        inputs: list of input directories/files
        output: output directory
        mapper: mapper command
        reducer: reducer command
        jar: path to Hadoop Streaming jar; ignored in local mode
        tasks: reduce task count
        partition field count: number of initial fields on which to partition
        key fields: number of key fields
        archives: -archives option
        multiple_outputs: True iff there are multiple outputs; else False
        inputformat: -inputformat option
        extra_args: extra '-D' args

        Return value: step dictionary
    """
    to_return = {
        'Name' : name,
        'ActionOnFailure' : action_on_failure,
        'HadoopJarStep' : {
            'Jar' : jar,
            'Args' : []
        }
    }
    to_return['HadoopJarStep']['Args'].extend(
            ['-D', 'mapreduce.job.reduces=%d' % tasks]
        )
    if partition_field_count is not None and key_fields is not None:
        assert key_fields >= partition_field_count
        to_return['HadoopJarStep']['Args'].extend([
            '-D', 'stream.num.map.output.key.fields=%d' % key_fields,
            '-D', 'mapreduce.partition.keypartitioner.options=-k1,%d'
                        % partition_field_count
        ])
        if key_fields != partition_field_count:
            to_return['HadoopJarStep']['Args'].extend([
                '-D', 'mapreduce.job.output.key.comparator.class='
                      'org.apache.hadoop.mapred.lib.KeyFieldBasedComparator',
                '-D', 'mapreduce.partition.keycomparator.options='
                      '-k1,%d -k%d,%d' % (partition_field_count,
                                                partition_field_count + 1, 
                                                key_fields)
            ])
    for extra_arg in extra_args:
        to_return['HadoopJarStep']['Args'].extend(
            ['-D', extra_arg]
        )
    # Add libjar for splittable LZO
    to_return['HadoopJarStep']['Args'].extend(
            ['-libjars', _relevant_elephant_jar]
        )
    if multiple_outputs:
        to_return['HadoopJarStep']['Args'][-1] \
            +=  (',%s' % _multiple_files_jar)
    if archives is not None:
        to_return['HadoopJarStep']['Args'].extend([
                '-archives', archives
            ])
    to_return['HadoopJarStep']['Args'].extend([
            '-partitioner',
            'org.apache.hadoop.mapred.lib.KeyFieldBasedPartitioner',
        ])
    to_return['HadoopJarStep']['Args'].extend([
                '-input', ','.join([an_input.strip() for an_input in inputs])
            ])
    to_return['HadoopJarStep']['Args'].extend([
            '-output', output,
            '-mapper', mapper,
            '-reducer', reducer
        ])
    if multiple_outputs:
        to_return['HadoopJarStep']['Args'].extend([
                '-outputformat', 'edu.jhu.cs.MultipleOutputFormat'
            ])
    if inputformat is not None:
        to_return['HadoopJarStep']['Args'].extend([
                '-inputformat', inputformat
            ])
    else:
        '''Always use splittable LZO; it's deprecated because hadoop-streaming
        uses the old mapred API.'''
        to_return['HadoopJarStep']['Args'].extend([
                '-inputformat',
                'com.twitter.elephantbird.mapred.input'
                '.DeprecatedCombineLzoTextInputFormat'
            ])
    return to_return

# TODO: Flesh out specification of protostep and migrate to Dooplicity
def steps(protosteps, action_on_failure, jar, step_dir, 
            reducer_count, intermediate_dir, extra_args=[], unix=False,
            no_consistent_view=False):
    """ Turns list with "protosteps" into well-formed StepConfig list.

        A protostep looks like this:

            {
                'name' : [name of step]
                'run' : Python script name; like 'preprocess.py' + args
                'inputs' : list of input directories
                'no_input_prefix' : key that's present iff intermediate dir
                    should not be prepended to inputs
                'output' : output directory
                'no_output_prefix' : key that's present iff intermediate dir
                    should not be prepended to output dir
                'keys'  : Number of key fields; present only if reducer
                'part'  : x from KeyFieldBasedPartitioner option -k1,x
                'min_tasks' : minimum number of reducer tasks
                'max_tasks' : maximum number of reducer tasks
                'taskx' : if present, override, min_tasks and max_tasks in
                    favor off taskx * reducer_count reducer tasks
                'inputformat' : input format; present only if necessary
                'archives' : archives parameter; present only if necessary
                'multiple_outputs' : key that's present iff there are multiple
                    outputs
                'index_output' : key that's present iff output LZOs should be
                    indexed after step; applicable only in Hadoop modes
                'direct_copy' : if output directory is s3, copy outputs there 
                    directly; do not use hdfs
                'extra_args' : list of '-D' args
            }

        protosteps: array of protosteps
        action_on_failure: action on failure to take
        jar: path to Hadoop Streaming jar
        step_dir: where to find Python scripts for steps
        reducer_count: number of reducers; determines number of tasks
        unix: performs UNIX-like path joins; also inserts pypy in for
            executable since unix=True only on EMR
        no_consistent_view: True iff consistent view should be switched off;
            adds s3distcp commands when there are multiple outputs

        Return value: list of StepConfigs (see Elastic MapReduce API docs)
    """
    '''CombineFileInputFormat outputs file offsets as keys; kill them here
    in an "identity" mapper. This could also be done by overriding an
    appropriate method from class in Java, but the overhead incurred doing
    it this way should be small.'''
    true_steps = []
    for protostep in protosteps:
        assert ('keys' in protostep and 'part' in protostep) or \
                ('keys' not in protostep and 'part' not in protostep)
        assert not (('direct_copy' in protostep) and ('multiple_outputs'
                        in protostep))
        identity_mapper = ('cut -f 2-' if unix else 'cat')
        final_output = (path_join(unix, intermediate_dir,
                                        protostep['output'])
                        if 'no_output_prefix' not in
                        protostep else protostep['output'])
        final_output_url = ab.Url(final_output)
        if (not ('direct_copy' in protostep) and unix
            and final_output_url.is_s3 and no_consistent_view):
            intermediate_output = _hdfs_temp_dir + final_output_url.suffix[1:]
        else:
            intermediate_output = final_output
        try:
            if not protostep['direct_copy']:
                intermediate_output \
                    = _hdfs_temp_dir + final_output_url.suffix[1:]
        except KeyError:
            pass
        assert 'taskx' in protostep or 'min_tasks' in protostep
        if 'taskx' in protostep:
            if protostep['taskx'] is None:
                reducer_task_count = 1
            else:
                reducer_task_count = reducer_count * protostep['taskx']
        elif protostep['min_tasks'] is None:
            reducer_task_count = reducer_count
        else:
            '''Task count logic: number of reduce tasks is equal to
            min(minimum number of tasks greater than
                min_tasks that's a multiple of the number of reducers,
                max_tasks). min_tasks = 0 and max_tasks = 0 if no reduces are
            to be performed. min_tasks must be specified, but max_tasks need
            not be specified.'''
            if not (protostep['min_tasks'] % reducer_count):
                reducer_task_count = protostep['min_tasks']
            else:
                reducer_task_count = \
                    protostep['min_tasks'] + reducer_count - (
                        (protostep['min_tasks'] + reducer_count)
                        % reducer_count
                    )
            if 'max_tasks' in protostep:
                reducer_task_count = min(reducer_task_count,
                                            protostep['max_tasks'])
        true_steps.append(step(
                name=protostep['name'],
                inputs=([path_join(unix, intermediate_dir,
                            an_input) for an_input in
                            protostep['inputs']]
                        if 'no_input_prefix' not in
                        protostep else protostep['inputs']),
                output=intermediate_output,
                mapper=' '.join(['pypy' if unix
                        else _executable, 
                        path_join(unix, step_dir,
                                        protostep['run'])])
                        if 'keys' not in protostep else identity_mapper,
                reducer=' '.join(['pypy' if unix
                        else _executable, 
                        path_join(unix, step_dir,
                                        protostep['run'])]) 
                        if 'keys' in protostep else 'cat',
                action_on_failure=action_on_failure,
                jar=jar,
                tasks=reducer_task_count,
                partition_field_count=(protostep['part']
                    if 'part' in protostep else None),
                key_fields=(protostep['keys']
                    if 'keys' in protostep else None),
                archives=(protostep['archives']
                    if 'archives' in protostep else None),
                multiple_outputs=(True if 'multiple_outputs'
                        in protostep else False
                    ),
                inputformat=(protostep['inputformat']
                    if 'inputformat' in protostep else None),
                extra_args=([extra_arg.format(task_count=reducer_count)
                    for extra_arg in protostep['extra_args']]
                    if 'extra_args' in protostep else [])
            )
        )
        if unix and 'index_output' in protostep:
            # Index inputs before subsequent step begins
            true_steps.append(
                    {
                        'Name' : ('Index output of "'
                                    + protostep['name'] + '"'),
                        'ActionOnFailure' : action_on_failure,
                        'HadoopJarStep' : {
                            'Jar' : _hadoop_lzo_jar,
                            'Args' : ['com.hadoop.compression.lzo'
                                      '.DistributedLzoIndexer',
                                      intermediate_output]
                        }
                    }
                )
        try:
            if not protostep['direct_copy']:
                true_steps.append(
                    {
                        'Name' : ('Copy output of "'
                                    + protostep['name'] + '" to S3'),
                        'ActionOnFailure' : action_on_failure,
                        'HadoopJarStep' : {
                            'Jar' : _s3distcp_jar,
                            'Args' : ['--src', intermediate_output,
                                      '--dest', final_output,
                                      '--deleteOnSuccess']
                        }
                    }
                )
        except KeyError:
            pass
        if (not ('direct_copy' in protostep) and unix
            and final_output_url.is_s3 and no_consistent_view):
            # s3distcp intermediates over
            true_steps.append(
                    {
                        'Name' : ('Copy output of "'
                                    + protostep['name'] + '" to S3'),
                        'ActionOnFailure' : action_on_failure,
                        'HadoopJarStep' : {
                            'Jar' : _s3distcp_jar,
                            'Args' : ['--src', intermediate_output,
                                      '--dest', final_output,
                                      '--deleteOnSuccess']
                        }
                    }
                )
    return true_steps

class RailRnaErrors(object):
    """ Holds accumulated errors in Rail-RNA's input parameters.

        Checks only those parameters common to all modes/job flows.
    """
    def __init__(self, manifest, output_dir,
            intermediate_dir='./intermediate', force=False, aws_exe=None,
            profile='default', region='us-east-1', verbose=False,
            curl_exe=None
        ):
        '''Store all errors uncovered in a list, then output. This prevents the
        user from having to rerun Rail-RNA to find what else is wrong with
        the command-line parameters.'''
        self.errors = []
        self.manifest_dir = None
        self.manifest = manifest
        self.output_dir = output_dir
        self.intermediate_dir = intermediate_dir
        self.aws_exe = aws_exe
        self.region = region
        self.force = force
        self.checked_programs = set()
        self.curl_exe = curl_exe
        self.verbose = verbose
        self.profile = profile

    def check_s3(self, reason=None, is_exe=None, which=None):
        """ Checks for AWS CLI and configuration file.

            In this script, S3 checking is performed as soon as it is found
            that S3 is needed. If anything is awry, a RuntimeError is raised
            _immediately_ (the standard behavior is to raise a RuntimeError
            only after errors are accumulated). A reason specifying where
            S3 credentials were first needed can also be provided.

            reason: string specifying where S3 credentials were first
                needed.

            No return value.
        """
        if not is_exe:
            is_exe = globals()['is_exe']
        if not which:
            which = globals()['which']
        original_errors_size = len(self.errors)
        if self.aws_exe is None:
            self.aws_exe = 'aws'
            if not which(self.aws_exe):
                self.errors.append(('The AWS CLI executable '
                                    'was not found. Make sure that the '
                                    'executable is in PATH, or specify the '
                                    'location of the executable with '
                                    '--aws.'))
        elif not is_exe(self.aws_exe):
            self.errors.append(('The AWS CLI executable (--aws) '
                                '"{0}" is either not present or not '
                                'executable.').format(aws_exe))
        self._aws_access_key_id = None
        self._aws_secret_access_key = None
        if self.profile == 'default':
            # Search environment variables for keys first if profile is default
            try:
                self._aws_access_key_id = os.environ['AWS_ACCESS_KEY_ID']
                self._aws_secret_access_key \
                    = os.environ['AWS_SECRET_ACCESS_KEY']
            except KeyError:
                to_search = '[default]'
            else:
                to_search = None
            try:
                # Also grab region
                self.region = os.environ['AWS_DEFAULT_REGION']
            except KeyError:
                pass
        else:
            to_search = '[profile ' + self.profile + ']'
        # Now search AWS CLI config file for the right profile
        if to_search is not None:
            cred_file = os.path.join(os.environ['HOME'], '.aws', 'cred')
            if os.path.exists(cred_file):
                # "credentials" file takes precedence over "config" file
                config_file = cred_file
            else:
                config_file = os.path.join(os.environ['HOME'], '.aws',
                                            'config')
            try:
                with open(config_file) as config_stream:
                    for line in config_stream:
                        if line.strip() == to_search:
                            break
                    for line in config_stream:
                        tokens = [token.strip() for token in line.split('=')]
                        if tokens[0] == 'region' \
                            and self.region == 'us-east-1':
                            self.region = tokens[1]
                        elif tokens[0] == 'aws_access_key_id':
                            self._aws_access_key_id = tokens[1]
                        elif tokens[0] == 'aws_secret_access_key':
                            self._aws_secret_access_key = tokens[1]
                        else:
                            line = line.strip()
                            if line[0] == '[' and line[-1] == ']':
                                # Break on start of new profile
                                break
            except IOError:
                self.errors.append(
                                   ('No valid AWS CLI configuration found. '
                                    'Make sure the AWS CLI is installed '
                                    'properly and that one of the following '
                                    'is true:\n\na) The environment variables '
                                    '"AWS_ACCESS_KEY_ID" and '
                                    '"AWS_SECRET_ACCESS_KEY" are set to '
                                    'the desired AWS access key ID and '
                                    'secret access key, respectively, and '
                                    'the profile (--profile) is set to '
                                    '"default" (its default value).\n\n'
                                    'b) The file ".aws/config" or '
                                    '".aws/credentials" exists in your '
                                    'home directory with a valid profile. '
                                    'To set this file up, run "aws --config" '
                                    'after installing the AWS CLI.')
                                )
        if len(self.errors) != original_errors_size:
            if reason:
                raise RuntimeError((('\n'.join(['%d) %s' % (i+1, error)
                                    for i, error
                                    in enumerate(self.errors)]) 
                                    if len(self.errors) > 1
                                    else self.errors[0]) + 
                                    '\n\nNote that the AWS CLI is needed '
                                    'because {0}. If all dependence on S3 in '
                                    'the pipeline is removed, the AWS CLI '
                                    'need not be installed.').format(reason))
            else:
                raise RuntimeError((('\n'.join(['%d) %s' % (i+1, error)
                                    for i, error
                                    in enumerate(self.errors)])
                                    if len(self.errors) > 1
                                    else self.errors[0]) + 
                                    '\n\nIf all dependence on S3 in the '
                                    'pipeline is removed, the AWS CLI need '
                                    'not be installed.'))
        self.checked_programs.add('AWS CLI')

    def check_program(self, exe, program_name, parameter,
                        entered_exe=None, reason=None,
                        is_exe=None, which=None):
        """ Checks if program in PATH or if user specified it properly.

            Errors are added to self.errors.

            exe: executable to search for
            program name: name of program
            parameter: corresponding command line parameter
                (e.g., --bowtie)
            entered_exe: None if the user didn't enter an executable; otherwise
                whatever the user entered
            reason: FOR CURL ONLY: raise RuntimeError _immediately_ if Curl
                not found but needed
            is_exe: is_exe function
            which: which function

            No return value.
        """
        if not is_exe:
            is_exe = globals()['is_exe']
        if not which:
            which = globals()['which']
        original_errors_size = len(self.errors)
        if entered_exe is None:
            if not which(exe):
                self.errors.append(
                        ('The executable "{0}" for {1} was either not found '
                         'in PATH or is not executable. Check that the '
                         'program is installed properly and executable; then '
                         'either add the executable to PATH or specify it '
                         'directly with {2}.').format(exe, program_name,
                                                            parameter)
                    )
            to_return = exe
        elif not is_exe(entered_exe):
            which_entered_exe = which(entered_exe)
            if which_entered_exe is None:
                self.errors.append(
                    ('The executable "{0}" entered for {1} via {2} was '
                     'either not found or is not executable.').format(exe,
                                                                program_name,
                                                                parameter)
                )
            to_return = which_entered_exe
        else:
            to_return = entered_exe
        if original_errors_size != len(self.errors) and reason:
            raise RuntimeError((('\n'.join(['%d) %s' % (i+1, error)
                                for i, error
                                in enumerate(self.errors)])
                                if len(self.errors) > 1 else self.errors[0]) + 
                                '\n\nNote that Curl is needed because {0}.'
                                ' If all dependence on web resources is '
                                'removed from the pipeline, Curl need '
                                'not be installed.').format(reason))
        self.checked_programs.add(program_name)
        return to_return

    @staticmethod
    def add_args(general_parser, exec_parser, required_parser):
        exec_parser.add_argument(
            '--aws', type=str, required=False, metavar='<exe>',
            default=None,
            help='path to AWS CLI executable (def: aws)'
        )
        exec_parser.add_argument(
            '--curl', type=str, required=False, metavar='<exe>',
            default=None,
            help='path to Curl executable (def: curl)'
        )
        general_parser.add_argument(
            '--profile', type=str, required=False, metavar='<str>',
            default='default',
            help='AWS CLI profile (def: env vars, then "default")'
        )
        general_parser.add_argument(
            '-f', '--force', action='store_const', const=True,
            default=False,
            help='overwrite output directory if it exists'
        )
        general_parser.add_argument(
            '--verbose', action='store_const', const=True,
            default=False,
            help='write extra debugging statements to stderr'
        )
        required_parser.add_argument(
            '-m', '--manifest', type=str, required=True, metavar='<file>',
            help='Myrna-style manifest file; Google "Myrna manifest" for ' \
                 'help'
        )
        '''--region's help looks different from mode to mode; don't include it
        here.'''

def raise_runtime_error(bases):
    """ Raises RuntimeError if any base.errors is nonempty.

        bases: dictionary mapping IPython engine IDs to RailRnaErrors
            instances or a single RailRnaError instance.

        No return value.
    """
    assert isinstance(bases, RailRnaErrors) or isinstance(bases, dict)
    if isinstance(bases, RailRnaErrors) and bases.errors:
        raise RuntimeError(
                '\n'.join(
                        ['%d) %s' % (i+1, error) for i, error
                            in enumerate(bases.errors)]
                    ) if len(bases.errors) > 1 else bases.errors[0]
            )
    elif isinstance(bases, dict):
        errors_to_report = defaultdict(set)
        for engine in bases:
            assert isinstance(bases[engine], RailRnaErrors)
            if bases[engine].errors:
                errors_to_report['\n'.join(
                            ['%d) %s' % (i+1, error) for i, error
                                in enumerate(bases[engine].errors)]
                        ) if len(bases[engine].errors) > 1
                          else bases[engine].errors[0]].add(engine)
        runtimeerror_message = []
        if errors_to_report:
            for message in errors_to_report:
                runtimeerror_message.extend(
                    ['Engine(s) %s report(s) the following errors.'
                        % engine_string_from_list(
                              errors_to_report[message]
                            ), message]
                    )
            raise RuntimeError('\n',join(errors_to_report))

def ipython_client(ipython_profile=None, ipcontroller_json=None):
    """ Performs checks on IPython engines and returns IPython Client object.

        Also checks that IPython is installed/configured properly and raises
        exception _immediately_ if it's not; then prints
        engine detection message.

        ipython_profile: IPython parallel profile, if specified; otherwise None
        ipcontroller_json: IP Controller json file, if specified; else None

        No return value.
    """
    errors = []
    try:
        from IPython.parallel import Client
    except ImportError:
        errors.append(
                   'IPython is required to run Rail-RNA in '
                   '"parallel" mode. Visit ipython.org to '
                   'download it, or simply download the Anaconda '
                   'distribution of Python at '
                   'https://store.continuum.io/cshop/anaconda/; it\'s '
                   'easy to install and comes with IPython and '
                   'several other useful packages.'
                )
    if ipython_profile:
        try:
            print sys.executable
            print ipython_profile
            rc = Client(profile=ipython_profile)
        except ValueError:
            errors.append(
                    'Cluster configuration profile "%s" was not '
                    'found.' % ipython_profile
                )
    elif ipcontroller_json:
        try:
            rc = Client(ipcontroller_json)
        except IOError:
            errors.append(
                    'Cannot find connection information JSON file %s.'
                    % ipcontroller_json
                )
    else:
        try:
            rc = Client()
        except IOError:
            errors.append(
                    'Cannot find ipcontroller-client.json. Ensure '
                    'that IPython controller and engines are running.'
                    ' If controller is running on a remote machine, '
                    'copy the ipcontroller-client.json file from there '
                    'to a local directory; then rerun this script '
                    'specifying the local path to '
                    'ipcontroller-client.json with the '
                    '--ipcontroller-json command-line parameter.'
                )
        except UnboundLocalError:
            # Client referenced before assignment; arises from ImportError
            pass
    if errors:
        raise RuntimeError(
                    '\n'.join(
                            ['%d) %s' % (i+1, error) for i, error
                                in enumerate(errors)]
                        ) if len(errors) > 1 else errors[0]
                )
    if not rc.ids:
        raise RuntimeError(
                'An IPython controller is running, but no engines are '
                'connected to it. Engines must be connected to an IPython '
                'controller when running Rail-RNA in "parallel" mode.'
            )
    print_to_screen('Detected %d running IPython engines.' 
                                % len(rc.ids))
    # Use Dill to permit general serializing
    try:
        import dill
    except ImportError:
        raise RuntimeError(
                'Rail-RNA requires Dill in "parallel" mode. Install it by '
                'running "pip install dill", or see the StackOverflow '
                'question http://stackoverflow.com/questions/23576969/'
                'how-to-install-dill-in-ipython for other leads.'
            )
    else:
        rc[:].use_dill()
    return rc

class RailRnaLocal(object):
    """ Checks local- or parallel-mode JSON for programs and input parameters.

        Subsumes only those parameters relevant to local mode. Adds errors
        to base instance of RailRnaErrors.
    """
    def __init__(self, base, check_manifest=False,
                    num_processes=1, keep_intermediates=False,
                    gzip_intermediates=False, gzip_level=3,
                    sort_memory_cap=(300*1024), max_task_attempts=4,
                    parallel=False,
                    local=True, scratch=None, ansible=None,
                    do_not_copy_index_to_nodes=False,
                    sort_exe=None):
        """ base: instance of RailRnaErrors """
        # Initialize ansible for easy checks
        if not ansible:
            ansible = ab.Ansible()
        if not ab.Url(base.intermediate_dir).is_local:
            base.errors.append(('Intermediate directory must be in locally '
                                'accessible filesystem when running Rail-RNA '
                                'in "local" or "parallel" mode, but {0} was '
                                'entered.').format(
                                        base.intermediate_dir
                                    ))
        else:
            base.intermediate_dir = os.path.abspath(base.intermediate_dir)
        output_dir_url = ab.Url(base.output_dir)
        if output_dir_url.is_curlable:
            base.errors.append(('Output directory must be in locally '
                                'accessible filesystem or on S3 '
                                'when running Rail-RNA in "local" '
                                'or "parallel" mode, '
                                'but {0} was entered.').format(
                                        base.output_dir
                                    ))
        elif output_dir_url.is_s3 and 'AWS CLI' not in base.checked_programs:
            base.check_s3(reason='the output directory is on S3',
                            is_exe=is_exe,
                            which=which)
            # Change ansible params
            ansible.aws_exe = base.aws_exe
            ansible.profile = base.profile
        base.check_s3_on_engines = None
        base.check_curl_on_engines = None
        base.do_not_copy_index_to_nodes = do_not_copy_index_to_nodes
        if not parallel:
            if output_dir_url.is_local \
                and os.path.exists(output_dir_url.to_url()):
                if not base.force:
                    base.errors.append(('Output directory {0} exists, '
                                        'and --force was not invoked to '
                                        'permit overwriting it.').format(
                                                base.output_dir)
                                            )
                else:
                    try:
                        shutil.rmtree(base.output_dir)
                    except OSError:
                        try:
                            os.remove(base.output_dir)
                        except OSError:
                            pass
                    base.output_dir = os.path.abspath(base.output_dir)
            elif output_dir_url.is_s3 \
                and ansible.s3_ansible.is_dir(base.output_dir):
                if not base.force:
                    base.errors.append(('Output directory {0} exists on S3, '
                                        'and --force was not invoked to '
                                        'permit overwriting it.').format(
                                                base_output_dir)
                                            )
                else:
                    ansible.s3ansible.remove_dir(base.output_dir)
            # Check manifest; download it if necessary
            manifest_url = ab.Url(base.manifest)
            if manifest_url.is_s3 and 'AWS CLI' not in base.checked_programs:
                base.check_s3(reason='the manifest file is on S3',
                                is_exe=is_exe,
                                which=which)
                # Change ansible params
                ansible.aws_exe = base.aws_exe
                ansible.profile = base.profile
            elif manifest_url.is_curlable \
                and 'Curl' not in base.checked_programs:
                base.curl_exe = base.check_program('curl', 'Curl', '--curl',
                                    entered_exe=base.curl_exe,
                                    reason='the manifest file is on the web',
                                    is_exe=is_exe,
                                    which=which)
                ansible.curl_exe = base.curl_exe
            if not ansible.exists(manifest_url.to_url()):
                base.errors.append(('Manifest file (--manifest) {0} '
                                    'does not exist. Check the URL and '
                                    'try again.').format(base.manifest))
            else:
                if not manifest_url.is_local:
                    '''Download/check manifest only if not an IPython engine
                    (not parallel).'''
                    base.manifest_dir = base.intermediate_dir
                    from tempdel import remove_temporary_directories
                    register_cleanup(remove_temporary_directories,
                                        [base.manifest_dir])
                    base.manifest = os.path.join(base.manifest_dir, 'MANIFEST')
                    ansible.get(manifest_url, destination=base.manifest)
                base.manifest = os.path.abspath(base.manifest)
                files_to_check = []
                base.sample_count = 0
                with open(base.manifest) as manifest_stream:
                    for line in manifest_stream:
                        if line[0] == '#' or not line.strip(): continue
                        base.sample_count += 1
                        tokens = line.strip().split('\t')
                        check_sample_label = True
                        if len(tokens) == 5:
                            files_to_check.extend([tokens[0], tokens[2]])
                        elif len(tokens) == 3:
                            files_to_check.append(tokens[0])
                        else:
                            base.errors.append(('The following line from the '
                                                'manifest file {0} '
                                                'has an invalid number of '
                                                'tokens:\n{1}'
                                                ).format(
                                                        manifest_url.to_url(),
                                                        line
                                                    ))
                            check_sample_label = False
                        if check_sample_label and tokens[-1].count('-') != 2:
                            line = line.strip()
                            base.errors.append(('The following line from the '
                                                'manifest file {0} '
                                                'has an invalid sample label: '
                                                '\n{1}\nA valid sample label '
                                                'takes the following form:\n'
                                                '<Group ID>-<BioRep ID>-'
                                                '<TechRep ID>'
                                                ).format(
                                                        manifest_url.to_url(),
                                                        line
                                                    ))
                if files_to_check:
                    if check_manifest:
                        # Check files in manifest only if in preprocess flow
                        file_count = len(files_to_check)
                        for k, filename in enumerate(files_to_check):
                            if sys.stdout.isatty():
                                sys.stdout.write(
                                        '\r\x1b[KChecking that file %d/%d '
                                        'from manifest file exists...' % (
                                                                    k+1,
                                                                    file_count
                                                                )
                                    )
                                sys.stdout.flush()
                            filename_url = ab.Url(filename)
                            if filename_url.is_s3 \
                                and 'AWS CLI' not in base.checked_programs:
                                    if local:
                                        base.check_s3(reason=(
                                                      'at least one sample '
                                                      'FASTA/FASTQ from the '
                                                      'manifest file is on '
                                                      'S3'),
                                                      is_exe=is_exe,
                                                      which=which
                                                    )
                                    base.check_s3_on_engines = (
                                                      'at least one sample '
                                                      'FASTA/FASTQ from the '
                                                      'manifest file is on '
                                                      'S3'
                                                    )
                                    # Change ansible params
                                    ansible.aws_exe = base.aws_exe
                                    ansible.profile = base.profile
                            elif filename_url.is_curlable \
                                and 'Curl' not in base.checked_programs:
                                if local:
                                    base.curl_exe = base.check_program('curl',
                                                    'Curl',
                                                    '--curl',
                                                    entered_exe=base.curl_exe,
                                                    reason=(
                                                      'at least one sample '
                                                      'FASTA/FASTQ from the '
                                                      'manifest file is on '
                                                      'the web'),
                                                    is_exe=is_exe,
                                                    which=which
                                                )
                                base.check_curl_on_engines = (
                                                      'at least one sample '
                                                      'FASTA/FASTQ from the '
                                                      'manifest file is on '
                                                      'the web'
                                                    )
                                ansible.curl_exe = base.curl_exe
                            if not ansible.exists(filename):
                                base.errors.append((
                                                    'The file {0} from the '
                                                    'manifest file {1} does '
                                                    'not exist. Check the URL '
                                                    'and try again.').format(
                                                        filename,
                                                        manifest_url.to_url()
                                                ))
                        if sys.stdout.isatty():
                            sys.stdout.write(
                                    '\r\x1b[KChecked all files listed in '
                                    'manifest file.\n'
                                )
                            sys.stdout.flush()
                else:
                    base.errors.append(('Manifest file (--manifest) {0} '
                                        'has no valid lines.').format(
                                                        manifest_url.to_url()
                                                    ))
            from multiprocessing import cpu_count
            if num_processes:
                if not (isinstance(num_processes, int)
                                        and num_processes >= 1):
                    base.errors.append('Number of processes (--num-processes) '
                                       'must be an integer >= 1, '
                                       'but {0} was entered.'.format(
                                                        num_processes
                                                    ))
                else:
                    base.num_processes = num_processes
            else:
                try:
                    base.num_processes = cpu_count()
                except NotImplementedError:
                    base.num_processes = 1
                if base.num_processes != 1:
                    '''Make default number of processes cpu count less 1
                    so Facebook tab in user's browser won't go all
                    unresponsive.'''
                    base.num_processes -= 1
            if gzip_intermediates:
                if not (isinstance(gzip_level, int)
                                        and 9 >= gzip_level >= 1):
                    base.errors.append('Gzip level (--gzip-level) '
                                       'must be an integer between 1 and 9, '
                                       'but {0} was entered.'.format(
                                                        gzip_level
                                                    ))
            base.gzip_intermediates = gzip_intermediates
            base.gzip_level = gzip_level
            if not (sort_memory_cap > 0):
                base.errors.append('Sort memory cap (--sort-memory-cap) '
                                   'must take a value larger than 0, '
                                   'but {0} was entered.'.format(
                                                        sort_memory_cap
                                                    ))
            base.sort_memory_cap = sort_memory_cap
            if not (isinstance(max_task_attempts, int)
                        and max_task_attempts >= 1):
                base.errors.append('Max task attempts (--max-task-attempts) '
                                   'must be an integer greater than 0, but '
                                   '{0} was entered'.format(
                                                        max_task_attempts
                                                    ))
            base.max_task_attempts = max_task_attempts
        if scratch:
            if not os.path.exists(scratch):
                try:
                    os.makedirs(scratch)
                except OSError:
                    base.errors.append(
                            ('Could not create scratch directory %s; '
                             'check that it\'s not a file and that '
                             'write permissions are active.') % scratch
                        )
        base.scratch = scratch
        if sort_exe:
            sort_exe_parameters = [parameter.strip()
                                    for parameter in sort_exe.split(' ')]
        else:
            sort_exe_parameters = []
        check_scratch = True
        try:
            sort_scratch = sort_exe_parameters[
                    sort_exe_parameters.index('--temporary-directory')+1
                ]
        except IndexError:
            base.errors.append(
                    ('"--temporary-directory" parameter was passed '
                     'to sort executable without specifying temporary '
                     'directory')
                )
        except ValueError:
            try:
                sort_scratch = sort_exe_parameters[
                        sort_exe_parameters.index('-T')+1
                    ]
            except IndexError:
                base.errors.append(
                    ('"-T" parameter was passed '
                     'to sort executable without specifying temporary '
                     'directory')
                )
            except ValueError:
                sort_scratch = base.scratch
                check_scratch = False
        if check_scratch:
            if not os.path.exists(sort_scratch):
                try:
                    os.makedirs(sort_scratch)
                except OSError:
                    base.errors.append(
                            ('Could not create sort scratch directory %s; '
                             'check that it\'s not a file and that '
                             'write permissions are active.')
                            % sort_scratch
                        )
        base.sort_exe = ' '.join(
                            [base.check_program('sort', 'sort', '--sort',
                                entered_exe=(sort_exe_parameters[0]
                                                if sort_exe_parameters
                                                else None),
                                is_exe=is_exe,
                                which=which)] + sort_exe_parameters[1:]
                             + (['-T', base.scratch] if (not check_scratch
                                 and base.scratch is not None) else [])
                        )

    @staticmethod
    def add_args(required_parser, general_parser, output_parser, 
                    exec_parser, prep=False, align=False, parallel=False):
        """ Adds parameter descriptions relevant to local mode to an object
            of class argparse.ArgumentParser.

            prep: preprocess-only
            align: align-only
            parallel: add parallel-mode arguments

            No return value.
        """
        exec_parser.add_argument(
            '--sort', type=str, required=False, metavar='<exe>',
            default=None,
            help=('path to sort executable; include extra sort parameters '
                  'here (def: sort)')
        )
        if align:
            required_parser.add_argument(
                '-i', '--input', type=str, required=True, metavar='<dir>',
                help='input directory with preprocessed reads; must be local'
            )
        if prep:
            output_parser.add_argument(
                '-o', '--output', type=str, required=False, metavar='<dir>',
                default='./rail-rna_prep',
                help='output directory; must be local or on S3'
            )
        else:
            output_parser.add_argument(
                '-o', '--output', type=str, required=False, metavar='<dir>',
                default='./rail-rna_out',
                help='output directory; must be local or on S3'
            )
        general_parser.add_argument(
            '--log', type=str, required=False, metavar='<dir>',
            default='./rail-rna_logs',
            help='directory for storing intermediate files and logs'
        )
        if not parallel:
            general_parser.add_argument(
               '-p', '--num-processes', type=int, required=False,
                metavar='<int>', default=None,
                help=('number of processes to run simultaneously (def: # cpus '
                      '- 1 if # cpus > 1; else 1)')
            )
            general_parser.add_argument(
                '--scratch', type=str, required=False, metavar='<dir>',
                default=None,
                help=('directory for storing temporary files (def: '
                      'securely created temporary directory)')
            )
        else:
            general_parser.add_argument(
                '--ipcontroller-json', type=str, required=False,
                metavar='<file>',
                default=None,
                help=('path to ipcontroller-client.json file '
                      '(def: IPython default for selected profile)')
            )
            general_parser.add_argument(
                '--ipython-profile', type=str, required=False, metavar='<str>',
                default=None,
                help=('connects to this IPython profile (def: default IPython '
                      'profile')
            )
            general_parser.add_argument(
                '--scratch', type=str, required=False, metavar='<dir>',
                default=None,
                help=('node-local scratch directory for storing Bowtie index '
                      'and temporary files before they are committed (def: '
                      'directory in /tmp and/or other securely created '
                      'temporary directory)')
            )
            if not prep:
                general_parser.add_argument(
                        '--do-not-copy-index-to-nodes', action='store_const',
                        const=True,
                        default=False,
                        help=('does not copy Bowtie/Bowtie 2 indexes to '
                              'nodes before starting job flow; copying '
                              'requires Herd')
                    )
        general_parser.add_argument(
            '--keep-intermediates', action='store_const', const=True,
            default=False,
            help='keep intermediate files in log directory after job flow ' \
                 'is complete'
        )
        general_parser.add_argument(
            '-g', '--gzip-intermediates', action='store_const', const=True,
            default=False,
            help='compress intermediate files; slower, but saves space'
        )
        general_parser.add_argument(
           '--gzip-level', type=int, required=False, metavar='<int>',
            default=3,
            help='level of gzip compression to use for intermediates, ' \
                 'if applicable'
        )
        general_parser.add_argument(
            '-r', '--sort-memory-cap', type=float, required=False,
            metavar='<dec>',
            default=(300*1024),
            help=('maximum amount of memory (in bytes) used by UNIX sort '
                  'per process')
        )
        general_parser.add_argument(
            '--max-task-attempts', type=int, required=False,
            metavar='<int>',
            default=(4 if parallel else 1),
            help=('maximum number of task attempts')
        )

class RailRnaElastic(object):
    """ Checks elastic-mode input parameters and relevant programs.

        Subsumes only those parameters relevant to elastic mode. Adds errors
        to base instance of RailRnaErrors.
    """
    def __init__(self, base, check_manifest=False,
        log_uri=None, ami_version='3.4.0',
        visible_to_all_users=False, tags='',
        name='Rail-RNA Job Flow',
        action_on_failure='TERMINATE_JOB_FLOW',
        hadoop_jar=None,
        master_instance_count=1, master_instance_type='c1.xlarge',
        master_instance_bid_price=None, core_instance_count=1,
        core_instance_type=None, core_instance_bid_price=None,
        task_instance_count=0, task_instance_type=None,
        task_instance_bid_price=None, ec2_key_name=None, keep_alive=False,
        termination_protected=False, no_consistent_view=False,
        intermediate_lifetime=4):

        # CLI is REQUIRED in elastic mode
        base.check_s3(reason='Rail-RNA is running in "elastic" mode')

        # Initialize possible options
        base.instance_core_counts = {
            "m1.small"    : 1,
            "m1.large"    : 2,
            "m1.xlarge"   : 4,
            "c1.medium"   : 2,
            "c1.xlarge"   : 8,
            "m2.xlarge"   : 2,
            "m2.2xlarge"  : 4,
            "m2.4xlarge"  : 8,
            "cc1.4xlarge" : 8,
            "m3.xlarge"   : 4,
            "m3.2xlarge"  : 8,
            "c3.2xlarge"  : 8,
            "c3.4xlarge"  : 16,
            "c3.8xlarge"  : 32
        }

        base.instance_mems = {
            "m1.small"    : (2*1024), #  1.7 GB
            "m1.large"    : (8*1024), #  7.5 GB
            "m1.xlarge"   : (16*1024), # 15.0 GB
            "c1.medium"   : (2*1024), #  1.7 GB
            "c1.xlarge"   : (8*1024), #  7.0 GB
            "m2.xlarge"   : (16*1024), # 17.1 GB
            "m2.2xlarge"  : (16*1024), # 34.2 GB
            "m2.4xlarge"  : (16*1024), # 68.4 GB
            "m3.xlarge"   : (15*1024),
            "m3.2xlarge"  : (30*1024),
            "cc1.4xlarge" : (16*1024), # 23.0 GB
            "c3.2xlarge" : (15*1024), # 15.0 GB
            "c3.4xlarge" : (30*1024), # 30 GB
            "c3.8xlarge" : (60*1024) # 60 GB
        }

        # From http://docs.aws.amazon.com/ElasticMapReduce/latest/
        # DeveloperGuide/TaskConfiguration_H2.html
        base.nodemanager_mems = {
            "m1.small"    : 1024,
            "m1.large"    : 3072,
            "m1.xlarge"   : 12288,
            "c1.medium"   : 1024,
            "c1.xlarge"   : 5120,
            "m2.xlarge"   : 14336,
            "m2.2xlarge"  : 30720,
            "m2.4xlarge"  : 61440,
            "cc1.4xlarge" : 20480,
            "m3.xlarge"   : 11520,
            "m3.2xlarge"  : 23040,
            "c3.2xlarge" : 11520,
            "c3.4xlarge" : 23040,
            "c3.8xlarge" : 53248
        }

        '''Not currently in use, but may become important if there are
        32- vs. 64-bit issues: base.instance_bits = {
            "m1.small"    : 32,
            "m1.large"    : 64,
            "m1.xlarge"   : 64,
            "c1.medium"   : 32,
            "c1.xlarge"   : 64,
            "m2.xlarge"   : 64,
            "m2.2xlarge"  : 64,
            "m2.4xlarge"  : 64,
            "cc1.4xlarge" : 64
        }'''

        if log_uri is not None and not Url(log_uri).is_s3:
            base.errors.append('Log URI (--log-uri) must be on S3, but '
                               '"{0}" was entered.'.format(log_uri))
        base.log_uri = log_uri
        base.visible_to_all_users = visible_to_all_users
        base.tags = [tag.strip() for tag in tags.split(',')]
        if len(base.tags) == 1 and base.tags[0] == '':
            base.tags = []
        base.name = name
        base.ami_version = ami_version
        # Initialize ansible for easy checks
        ansible = ab.Ansible(aws_exe=base.aws_exe, profile=base.profile)
        output_dir_url = ab.Url(base.output_dir)
        if not output_dir_url.is_s3:
            base.errors.append(('Output directory (--output) must be on S3 '
                                'when running Rail-RNA in "elastic" '
                                'mode, but {0} was entered.').format(
                                        base.output_dir
                                    ))
        if base.intermediate_dir is None:
            base.intermediate_dir = base.output_dir + '.intermediate'
        intermediate_dir_url = ab.Url(base.intermediate_dir)
        if intermediate_dir_url.is_local:
            base.errors.append(('Intermediate directory (--intermediate) '
                                'must be on HDFS or S3 when running Rail-RNA '
                                'in "elastic" mode, '
                                'but {0} was entered.').format(
                                        base.intermediate_dir
                                    ))
        elif intermediate_dir_url.is_s3:
            if not (isinstance(intermediate_lifetime, int) and
                        intermediate_lifetime != 0):
                base.errors.append(('Intermediate lifetime '
                                    '(--intermediate-lifetime) must be '
                                    '-1 or > 0, but {0} was entered.').format(
                                            intermediate_lifetime
                                        ))
            else:
                # Set up rule on S3 for deleting intermediate dir
                final_intermediate_dir = intermediate_dir_url.to_url() + '/'
                while final_intermediate_dir[-2] == '/':
                    final_intermediate_dir = final_intermediate_dir[:-1]
                ansible.s3_ansible.expire_prefix(final_intermediate_dir,
                                                    days=intermediate_lifetime)
        if ansible.s3_ansible.is_dir(base.output_dir):
            if not base.force:
                base.errors.append(('Output directory {0} exists on S3, and '
                                    '--force was not invoked to permit '
                                    'overwriting it.').format(base.output_dir))
            else:
                ansible.s3_ansible.remove_dir(base.output_dir)
        # Check manifest; download it if necessary
        manifest_url = ab.Url(base.manifest)
        if manifest_url.is_curlable \
            and 'Curl' not in base.checked_programs:
            base.curl_exe = base.check_program('curl', 'Curl', '--curl',
                                    entered_exe=base.curl_exe,
                                    reason='the manifest file is on the web')
            ansible.curl_exe = base.curl_exe
        if not ansible.exists(manifest_url.to_url()):
            base.errors.append(('Manifest file (--manifest) {0} '
                                'does not exist. Check the URL and '
                                'try again.').format(base.manifest))
        else:
            if not manifest_url.is_local:
                temp_manifest_dir = tempfile.mkdtemp()
                from tempdel import remove_temporary_directories
                register_cleanup(remove_temporary_directories,
                                    [temp_manifest_dir])
                manifest = os.path.join(temp_manifest_dir, 'MANIFEST')
                ansible.get(base.manifest, destination=manifest)
            else:
                manifest = manifest_url.to_url()
            files_to_check = []
            base.sample_count = 0
            with open(manifest) as manifest_stream:
                for line in manifest_stream:
                    if line[0] == '#' or not line.strip(): continue
                    base.sample_count += 1
                    tokens = line.strip().split('\t')
                    check_sample_label = True
                    if len(tokens) == 5:
                        files_to_check.extend([tokens[0], tokens[2]])
                    elif len(tokens) == 3:
                        files_to_check.append(tokens[0])
                    else:
                        check_sample_label = False
                        base.errors.append(('The following line from the '
                                            'manifest file {0} '
                                            'has an invalid number of '
                                            'tokens:\n{1}'
                                            ).format(
                                                    manifest_url.to_url(),
                                                    line
                                                ))
                    if check_sample_label and tokens[-1].count('-') != 2:
                        base.errors.append(('The following line from the '
                                            'manifest file {0} '
                                            'has an invalid sample label: '
                                            '\n{1}\nA valid sample label '
                                            'takes the following form:\n'
                                            '<Group ID>-<BioRep ID>-'
                                            '<TechRep ID>'
                                            ).format(
                                                    manifest_url.to_url(),
                                                    line
                                                ))
            if files_to_check:
                if check_manifest:
                    file_count = len(files_to_check)
                    # Check files in manifest only if in preprocess job flow
                    for k, filename in enumerate(files_to_check):
                        if sys.stdout.isatty():
                            sys.stdout.write(
                                    '\r\x1b[KChecking that file %d/%d '
                                    'from manifest file exists...' % (
                                                                k+1,
                                                                file_count
                                                            )
                                )
                            sys.stdout.flush()
                        filename_url = ab.Url(filename)
                        if filename_url.is_curlable \
                            and 'Curl' not in base.checked_programs:
                            base.curl_exe = base.check_program('curl', 'Curl',
                                                '--curl',
                                                entered_exe=base.curl_exe,
                                                reason=('at least one sample '
                                                  'FASTA/FASTQ from the '
                                                  'manifest file is on '
                                                  'the web'))
                            ansible.curl_exe = base.curl_exe
                        if not ansible.exists(filename_url.to_url()):
                            base.errors.append(('The file {0} from the '
                                                'manifest file {1} does not '
                                                'exist; check the URL and try '
                                                'again.').format(
                                                        filename,
                                                        manifest_url.to_url()
                                                    ))
                    if sys.stdout.isatty():
                        sys.stdout.write(
                                '\r\x1b[KChecked all files listed in manifest '
                                'file.\n'
                            )
                        sys.stdout.flush()
            else:
                base.errors.append(('Manifest file (--manifest) {0} '
                                    'has no valid lines.').format(
                                                        manifest_url.to_url()
                                                    ))
            if not manifest_url.is_s3 and output_dir_url.is_s3:
                # Copy manifest file to S3 before job flow starts
                base.manifest = path_join(True, base.output_dir + '.manifest',
                                                'MANIFEST')
                ansible.put(manifest, base.manifest)
            if not manifest_url.is_local:
                # Clean up
                shutil.rmtree(temp_manifest_dir)

        actions_on_failure \
            = set(['TERMINATE_JOB_FLOW', 'CANCEL_AND_WAIT', 'CONTINUE',
                    'TERMINATE_CLUSTER'])

        if action_on_failure not in actions_on_failure:
            base.errors.append('Action on failure (--action-on-failure) '
                               'must be one of {{"TERMINATE_JOB_FLOW", '
                               '"CANCEL_AND_WAIT", "CONTINUE", '
                               '"TERMINATE_CLUSTER"}}, but '
                               '{0} was entered.'.format(
                                                action_on_failure
                                            ))
        base.action_on_failure = action_on_failure
        if hadoop_jar is None:
            base.hadoop_jar = _hadoop_streaming_jar
        else:
            base.hadoop_jar = hadoop_jar
        instance_type_message = ('Instance type (--instance-type) must be '
                                 'in the set {{"m1.small", "m1.large", '
                                 '"m1.xlarge", "c1.medium", "c1.xlarge", '
                                 '"m2.xlarge", "m2.2xlarge", "m2.4xlarge", '
                                 '"cc1.4xlarge"}}, but {0} was entered.')
        if master_instance_type not in base.instance_core_counts:
            base.errors.append(('Master instance type '
                               '(--master-instance-type) not valid. %s')
                                % instance_type_message.format(
                                                        master_instance_type
                                                    ))
        base.master_instance_type = master_instance_type
        if core_instance_type is None:
            base.core_instance_type = base.master_instance_type
        else:
            if core_instance_type not in base.instance_core_counts:
                base.errors.append(('Core instance type '
                                    '(--core-instance-type) not valid. %s')
                                    % instance_type_message.format(
                                                        core_instance_type
                                                    ))
            base.core_instance_type = core_instance_type
        if task_instance_type is None:
            base.task_instance_type = base.master_instance_type
        else:
            if task_instance_type not in base.instance_core_counts:
                base.errors.append(('Task instance type '
                                    '(--task-instance-type) not valid. %s')
                                    % instance_type_message.format(
                                                        task_instance_type
                                                    ))
            base.task_instance_type = task_instance_type
        if master_instance_bid_price is None:
            base.spot_master = False
        else:
            if not (master_instance_bid_price > 0):
                base.errors.append('Spot instance bid price for master nodes '
                                   '(--master-instance-bid-price) must be '
                                   '> 0, but {0} was entered.'.format(
                                                    master_instance_bid_price
                                                ))
            base.spot_master = True
        base.master_instance_bid_price = master_instance_bid_price
        if core_instance_bid_price is None:
            base.spot_core = False
        else:
            if not (core_instance_bid_price > 0):
                base.errors.append('Spot instance bid price for core nodes '
                                   '(--core-instance-bid-price) must be '
                                   '> 0, but {0} was entered.'.format(
                                                    core_instance_bid_price
                                                ))
            base.spot_core = True
        base.core_instance_bid_price = core_instance_bid_price
        if task_instance_bid_price is None:
            base.spot_task = False
        else:
            if not (task_instance_bid_price > 0):
                base.errors.append('Spot instance bid price for task nodes '
                                   '(--task-instance-bid-price) must be '
                                   '> 0, but {0} was entered.'.format(
                                                    task_instance_bid_price
                                                ))
            base.spot_task = True
        base.task_instance_bid_price = task_instance_bid_price
        if not (isinstance(master_instance_count, int)
                and master_instance_count >= 1):
            base.errors.append('Master instance count '
                               '(--master-instance-count) must be an '
                               'integer >= 1, but {0} was entered.'.format(
                                                    master_instance_count
                                                ))
        base.master_instance_count = master_instance_count
        if not (isinstance(core_instance_count, int)
                 and core_instance_count >= 1):
            base.errors.append('Core instance count '
                               '(--core-instance-count) must be an '
                               'integer >= 1, but {0} was entered.'.format(
                                                    core_instance_count
                                                ))
        base.core_instance_count = core_instance_count
        if not (isinstance(task_instance_count, int)
                and task_instance_count >= 0):
            base.errors.append('Task instance count '
                               '(--task-instance-count) must be an '
                               'integer >= 1, but {0} was entered.'.format(
                                                    task_instance_count
                                                ))
        base.task_instance_count = task_instance_count
        # Raise exceptions before computing mems
        raise_runtime_error(base)
        if base.core_instance_count > 0:
            base.mem \
                = base.instance_mems[base.core_instance_type]
            base.nodemanager_mem \
                = base.nodemanager_mems[base.core_instance_type]
            base.max_tasks \
                = base.instance_core_counts[base.core_instance_type]
            base.total_cores = (base.core_instance_count
                * base.instance_core_counts[base.core_instance_type]
                + base.task_instance_count
                * base.instance_core_counts[base.task_instance_type])
        else:
            base.mem \
                = base.instance_mems[base.master_instance_type]
            base.nodemanager_mem \
                = base.nodemanager_mems[base.master_instance_type]
            base.max_tasks \
                = base.instance_core_counts[base.master_instance_type]
            base.total_cores \
                = base.instance_core_counts[base.master_instance_type]
        base.ec2_key_name = ec2_key_name
        base.keep_alive = keep_alive
        base.termination_protected = termination_protected
        base.original_no_consistent_view = no_consistent_view
        if no_consistent_view and base.region != 'us-east-1':
            # Read-after-write consistency is guaranteed
            base.no_consistent_view = False
        else:
            base.no_consistent_view = True

    @staticmethod
    def add_args(general_parser, required_parser, output_parser, 
                    elastic_parser, align=False):
        if align:
            required_parser.add_argument(
                '-i', '--input', type=str, required=True, metavar='<s3_dir>',
                help='input directory with preprocessed reads; must begin ' \
                     'with s3://'
            )
        required_parser.add_argument(
            '-o', '--output', type=str, required=True, metavar='<s3_dir>',
            help='output directory; must begin with s3://'
        )
        general_parser.add_argument(
            '--intermediate', type=str, required=False,
            metavar='<s3_dir/hdfs_dir>',
            default=None,
            help='directory for storing intermediate files; can begin with ' \
                 'hdfs:// or s3://; use S3 and set --intermediate-lifetime ' \
                 'to -1 to keep intermediates (def: output directory + ' \
                 '".intermediate")'
        )
        elastic_parser.add_argument(
            '--intermediate-lifetime', type=int, required=False,
            metavar='<int>',
            default=4,
            help='create rule for deleting intermediate files on S3 in ' \
                 'specified number of days; use -1 to keep intermediates'
        )
        elastic_parser.add_argument('--name', type=str, required=False,
            metavar='<str>',
            default='Rail-RNA Job Flow',
            help='job flow name'
        )
        elastic_parser.add_argument('--log-uri', type=str, required=False,
            metavar='<s3_dir>',
            default=None,
            help=('Hadoop log directory on S3 (def: output directory + '
                  '".logs")')
        )
        elastic_parser.add_argument('--ami-version', type=str, required=False,
            metavar='<str>',
            default='3.4.0',
            help='Amazon Machine Image to use'
        )
        elastic_parser.add_argument('--visible-to-all-users',
            action='store_const',
            const=True,
            default=False,
            help='make EC2 cluster visible to all IAM users within EMR CLI'
        )
        elastic_parser.add_argument('--action-on-failure', type=str,
            required=False,
            metavar='<choice>',
            default='TERMINATE_JOB_FLOW',
            help=('action to take if job flow fails on a given step. '
                  '<choice> is in {"TERMINATE_JOB_FLOW", "CANCEL_AND_WAIT", '
                  '"CONTINUE", "TERMINATE_CLUSTER"}')
        )
        elastic_parser.add_argument('--no-consistent-view',
            action='store_const',
            const=True,
            default=False,
            help=('do not use "consistent view," which incurs DynamoDB '
                 'charges; some intermediate data may then (very rarely) '
                 'be lost'))
        elastic_parser.add_argument('--hadoop-jar', type=str, required=False,
            metavar='<jar>',
            default=None,
            help=('Hadoop Streaming Java ARchive to use (def: AMI default)')
        )
        elastic_parser.add_argument('--master-instance-count', type=int,
            metavar='<int>',
            required=False,
            default=1,
            help=('number of master instances')
        )
        required_parser.add_argument('-c', '--core-instance-count', type=int,
            metavar='<int>',
            required=True,
            help=('number of core instances')
        )
        elastic_parser.add_argument('--task-instance-count', type=int,
            metavar='<int>',
            required=False,
            default=0,
            help=('number of task instances')
        )
        elastic_parser.add_argument('--master-instance-bid-price', type=float,
            metavar='<dec>',
            required=False,
            default=None,
            help=('bid price (dollars/hr); invoke only if master instances '
                  'should be spot')
        )
        elastic_parser.add_argument('--core-instance-bid-price', type=float,
            metavar='<dec>',
            required=False,
            default=None,
            help=('bid price (dollars/hr); invoke only if core instances '
                  'should be spot')
        )
        elastic_parser.add_argument('--task-instance-bid-price', type=float,
            metavar='<dec>',
            required=False,
            default=None,
            help=('bid price (dollars/hr); invoke only if task instances '
                  'should be spot')
        )
        elastic_parser.add_argument('--master-instance-type', type=str,
            metavar='<choice>',
            required=False,
            default='c3.2xlarge',
            help=('master instance type')
        )
        elastic_parser.add_argument('--core-instance-type', type=str,
            metavar='<choice>',
            required=False,
            default=None,
            help=('core instance type')
        )
        elastic_parser.add_argument('--task-instance-type', type=str,
            metavar='<choice>',
            required=False,
            default=None,
            help=('task instance type')
        )
        elastic_parser.add_argument('--ec2-key-name', type=str,
            metavar='<str>',
            required=False,
            default=None,
            help=('key pair name for SSHing to EC2 instances (def: '
                  'unspecified, so SSHing is not permitted)')
        )
        elastic_parser.add_argument('--keep-alive', action='store_const',
            const=True,
            default=False,
            help=('keep cluster alive after job flow completes')
        )
        elastic_parser.add_argument('--termination-protected',
            action='store_const',
            const=True,
            default=False,
            help=('protect cluster from termination in case of step failure')
        )
        elastic_parser.add_argument('--region', type=str,
            metavar='<choice>',
            required=False,
            default='us-east-1',
            help=('Amazon data center in which to run job flow. Google '
                  '"Elastic MapReduce regions" for recent list of centers ')
        )

    @staticmethod
    def hadoop_debugging_steps(base):
        return [
            {
                'ActionOnFailure' : base.action_on_failure,
                'HadoopJarStep' : {
                    'Args' : [
                        ('s3://%s.elasticmapreduce/libs/'
                         'state-pusher/0.1/fetch') % base.region
                    ],
                    'Jar' : ('s3://%s.elasticmapreduce/libs/'
                             'script-runner/script-runner.jar') % base.region
                },
                'Name' : 'Set up Hadoop Debugging'
            }
        ]

    @staticmethod
    def misc_steps(base):
        return [
            {
                'ActionOnFailure' : base.action_on_failure,
                'HadoopJarStep' : {
                    'Args' : [
                        '--src,s3://rail-emr/index/hg19_UCSC.tar.gz',
                        '--dest,hdfs:///index/'
                    ],
                    'Jar' : '/home/hadoop/lib/emr-s3distcp-1.0.jar'
                },
                'Name' : 'Copy Bowtie indexes from S3 to HDFS'
            }
        ]

    @staticmethod
    def bootstrap(base):
        return [
            {
                'Name' : 'Allocate swap space',
                'ScriptBootstrapAction' : {
                    'Args' : [
                        '%d' % base.mem
                    ],
                    'Path' : 's3://elasticmapreduce/bootstrap-actions/add-swap'
                }
            },
            {
                'Name' : 'Configure Hadoop',
                'ScriptBootstrapAction' : {
                    'Args' : [
                        '-c',
                        'fs.s3n.multipart.uploads.enabled=true',
                        '-y',
                        'yarn.nodemanager.pmem-check-enabled=false',
                        '-y',
                        'yarn.nodemanager.vmem-check-enabled=false',
                        '-y',
                        'yarn.nodemanager.resource.memory-mb=%d'
                        % base.nodemanager_mem,
                        '-y',
                        'yarn.scheduler.minimum-allocation-mb=%d'
                        % (base.nodemanager_mem / base.max_tasks),
                        '-y',
                        'yarn.nodemanager.vmem-pmem-ratio=2.1',
                        '-y',
                        'yarn.nodemanager.container-manager.thread-count=1',
                        '-y',
                        'yarn.nodemanager.localizer.fetch.thread-count=1',
                        '-m',
                        'mapreduce.map.speculative=true',
                        '-m',
                        'mapreduce.reduce.speculative=true',
                        '-m',
                        'mapreduce.map.memory.mb=%d'
                        % (base.nodemanager_mem / base.max_tasks),
                        '-m',
                        'mapreduce.reduce.memory.mb=%d'
                        % (base.nodemanager_mem / base.max_tasks),
                        '-m',
                        'mapreduce.map.java.opts=-Xmx%dm'
                        % (base.nodemanager_mem / base.max_tasks * 8 / 10),
                        '-m',
                        'mapreduce.reduce.java.opts=-Xmx%dm'
                        % (base.nodemanager_mem / base.max_tasks * 8 / 10),
                        '-m',
                        'mapreduce.map.cpu.vcores=1',
                        '-m',
                        'mapreduce.reduce.cpu.vcores=1',
                        '-m',
                        'mapred.output.compress=true',
                        '-m',
                        ('mapreduce.output.fileoutputformat.compress.codec='
                         'com.hadoop.compression.lzo.LzopCodec'),
                        '-m',
                        'mapreduce.job.maps=%d' % base.total_cores,
                    ] + (['-e', 'fs.s3.consistent=true']
                            if not base.original_no_consistent_view
                            else ['-e', 'fs.s3.consistent=false']),
                    'Path' : ('s3://%s.elasticmapreduce/bootstrap-actions/'
                              'configure-hadoop' % base.region)
                }
            }
        ]

    @staticmethod
    def instances(base):
        assert base.master_instance_count >= 1
        to_return = {
            'HadoopVersion' : '2.4.0',
            'InstanceGroups' : [
                {
                    'InstanceCount' : base.master_instance_count,
                    'InstanceRole' : 'MASTER',
                    'InstanceType': base.master_instance_type,
                    'Name' : 'Master Instance Group'
                }
            ],
            'KeepJobFlowAliveWhenNoSteps': ('true' if base.keep_alive
                                               else 'false'),
            'TerminationProtected': ('true' if base.termination_protected
                                        else 'false')
        }
        if base.master_instance_bid_price is not None:
            to_return['InstanceGroups'][0]['BidPrice'] \
                = '%0.03f' % base.master_instance_bid_price
            to_return['InstanceGroups'][0]['Market'] \
                = 'SPOT'
        else:
            to_return['InstanceGroups'][0]['Market'] \
                = 'ON_DEMAND'
        if base.core_instance_count:
            to_return['InstanceGroups'].append(
                    {
                        'InstanceCount' : base.core_instance_count,
                        'InstanceRole' : 'CORE',
                        'InstanceType': base.core_instance_type,
                        'Name' : 'Core Instance Group'
                    }
                )
            if base.core_instance_bid_price is not None:
                to_return['InstanceGroups'][1]['BidPrice'] \
                    = '%0.03f' % base.core_instance_bid_price
                to_return['InstanceGroups'][1]['Market'] \
                    = 'SPOT'
            else:
                to_return['InstanceGroups'][1]['Market'] \
                    = 'ON_DEMAND'
        if base.task_instance_count:
            to_return['InstanceGroups'].append(
                    {
                        'InstanceCount' : base.task_instance_count,
                        'InstanceRole' : 'TASK',
                        'InstanceType': base.task_instance_type,
                        'Name' : 'Task Instance Group'
                    }
                )
            if base.task_instance_bid_price is not None:
                to_return['InstanceGroups'][1]['BidPrice'] \
                    = '%0.03f' % base.task_instance_bid_price
                to_return['InstanceGroups'][1]['Market'] \
                    = 'SPOT'
            else:
                to_return['InstanceGroups'][1]['Market'] \
                    = 'ON_DEMAND'
        if base.ec2_key_name is not None:
            to_return['Ec2KeyName'] = base.ec2_key_name
        return to_return

class RailRnaPreprocess(object):
    """ Sets parameters relevant to just the preprocessing step of a job flow.
    """
    def __init__(self, base, nucleotides_per_input=8000000, gzip_input=True):
        if not (isinstance(nucleotides_per_input, int) and
                nucleotides_per_input > 0):
            base.errors.append('Nucleotides per input '
                               '(--nucleotides-per-input) must be an integer '
                               '> 0, but {0} was entered.'.format(
                                                        nucleotides_per_input
                                                       ))
        base.nucleotides_per_input = nucleotides_per_input
        base.gzip_input = gzip_input

    @staticmethod
    def add_args(general_parser, output_parser, elastic=False):
        """ Adds parameter descriptions relevant to preprocess job flow to an
            object of class argparse.ArgumentParser.

            No return value.
        """
        if not elastic:
            output_parser.add_argument(
                '--nucleotides-per-input', type=int, required=False,
                metavar='<int>',
                default=100000000,
                help='max nucleotides from input reads to assign to each task'
            )
            output_parser.add_argument(
                '--do-not-gzip-input', action='store_const', const=True,
                default=False,
                help=('leave preprocessed input reads uncompressed')
            )
        general_parser.add_argument(
            '--do-not-check-manifest', action='store_const', const=True,
            default=False,
            help='do not check that files listed in manifest file exist'
        )

    @staticmethod
    def protosteps(base, prep_dir, push_dir, elastic=False):
        if not elastic:
            steps_to_return = [
                {
                    'name' : 'Count lines in input files',
                    'run' : 'count_inputs.py',
                    'inputs' : [base.old_manifest
                                if hasattr(base, 'old_manifest')
                                else base.manifest],
                    'no_input_prefix' : True,
                    'output' : 'count_lines',
                    'inputformat' : (
                           'org.apache.hadoop.mapred.lib.NLineInputFormat'
                        ),
                    'min_tasks' : 0,
                    'max_tasks' : 0,
                    'direct_copy' : True
                },
                {
                    'name' : 'Assign reads to preprocessing tasks',
                    'run' : ('assign_splits.py --num-processes {0}'
                             ' --out {1} --filename {2} {3}').format(
                                                        base.num_processes,
                                                        base.intermediate_dir,
                                                        'split.manifest',
                                                        ('--scratch %s' %
                                                          base.scratch)
                                                        if base.scratch
                                                        is not None
                                                        else ''
                                                    ),
                    'inputs' : ['count_lines'],
                    'output' : 'assign_reads',
                    'min_tasks' : 1,
                    'max_tasks' : 1,
                    'keys' : 1,
                    'part' : 1,
                    'direct_copy' : True
                },
                {
                    'name' : 'Preprocess reads',
                    'run' : ('preprocess.py --nucs-per-file={0} {1} '
                             '--push={2} --gzip-level {3} {4} {5}').format(
                                                    base.nucleotides_per_input,
                                                    '--gzip-output' if
                                                    base.gzip_input else '',
                                                    push_dir,
                                                    base.gzip_level if
                                                    'gzip_level' in
                                                    dir(base) else 3,
                                                    '--stdout' if elastic
                                                    else '',
                                                    ('--scratch %s' %
                                                      base.scratch) if 
                                                    base.scratch is not None
                                                    else ''
                                                ),
                    'inputs' : [os.path.join(base.intermediate_dir,
                                                'split.manifest')],
                    'no_input_prefix' : True,
                    'output' : push_dir if elastic else prep_dir,
                    'no_output_prefix' : True,
                    'inputformat' : (
                           'org.apache.hadoop.mapred.lib.NLineInputFormat'
                        ),
                    'min_tasks' : 0,
                    'max_tasks' : 0,
                    'index_output' : True,
                    'direct_copy' : True
                },
            ]
        else:
            steps_to_return = [
                {
                    'name' : 'Preprocess reads',
                    'run' : ('preprocess.py --nucs-per-file={0} {1} '
                             '--push={2} --gzip-level {3} {4}').format(
                                                    base.nucleotides_per_input,
                                                    '--gzip-output' if
                                                    base.gzip_input else '',
                                                    ab.Url(push_dir).to_url(
                                                            caps=True
                                                        ),
                                                    base.gzip_level if
                                                    'gzip_level' in
                                                    dir(base) else 3,
                                                    '--stdout' if elastic
                                                    else ''
                                                ),
                    'inputs' : [base.old_manifest
                                if hasattr(base, 'old_manifest')
                                else base.manifest],
                    'no_input_prefix' : True,
                    'output' : push_dir if elastic else prep_dir,
                    'no_output_prefix' : True,
                    'inputformat' : (
                           'org.apache.hadoop.mapred.lib.NLineInputFormat'
                        ),
                    'min_tasks' : 0,
                    'max_tasks' : 0,
                    'direct_copy' : True
                },
            ]
        return steps_to_return

    @staticmethod
    def bootstrap():
        return [
            {
                'Name' : 'Install PyPy',
                'ScriptBootstrapAction' : {
                    'Args' : [
                        ('s3://rail-emr/bin/'
                         'pypy-2.2.1-linux_x86_64-portable.tar.bz2')
                    ],  
                    'Path' : 's3://rail-emr/bootstrap/install-pypy.sh'
                }
            },
            {
                'Name' : 'Install Rail-RNA',
                'ScriptBootstrapAction' : {
                    'Args' : [
                        's3://rail-emr/bin/rail-rna-%s.tar.gz' 
                        % version_number,
                        '/mnt'
                    ],
                    'Path' : 's3://rail-emr/bootstrap/install-rail.sh'
                }
            }
        ]

class RailRnaAlign(object):
    """ Sets parameters relevant to just the "align" job flow. """
    def __init__(self, base, input_dir=None, elastic=False,
        bowtie1_exe=None, bowtie1_idx='genome', bowtie1_build_exe=None,
        bowtie2_exe=None, bowtie2_build_exe=None, bowtie2_idx='genome',
        bowtie2_args='', samtools_exe=None, bedgraphtobigwig_exe=None,
        genome_partition_length=5000, max_readlet_size=25,
        readlet_config_size=32, min_readlet_size=15, readlet_interval=4,
        cap_size_multiplier=1.2, max_intron_size=500000, min_intron_size=10,
        min_exon_size=9, search_filter='none',
        motif_search_window_size=1000, max_gaps_mismatches=3,
        motif_radius=5, genome_bowtie1_args='-v 0 -a -m 80',
        transcriptome_bowtie2_args='-k 30', count_multiplier=15,
        intron_confidence_criteria='0.5,5', tie_margin=6,
        normalize_percentile=0.75, transcriptome_indexes_per_sample=500,
        drop_deletions=False, do_not_output_bam_by_chr=False, output_sam=False,
        bam_basename='alignments', bed_basename='', assembly='hg19',
        s3_ansible=None):
        if not elastic:
            '''Programs and Bowtie indices should be checked only in local
            mode.'''
            base.bowtie1_exe = base.check_program('bowtie', 'Bowtie 1',
                                '--bowtie1', entered_exe=bowtie1_exe,
                                is_exe=is_exe, which=which)
            bowtie1_version_command = [base.bowtie1_exe, '--version']
            try:
                base.bowtie1_version = subprocess.check_output(
                        bowtie1_version_command
                    ).split('\n', 1)[0].split(' ')[-1]
            except Exception as e:
                base.errors.append(('Error "{0}" encountered attempting to '
                                    'execute "{1}".').format(
                                                        e.message,
                                                        ' '.join(
                                                        bowtie1_version_command
                                                       )
                                                    ))
            base.bowtie1_build_exe = base.check_program('bowtie-build',
                                            'Bowtie 1 Build',
                                            '--bowtie1-build',
                                            entered_exe=bowtie1_build_exe,
                                            is_exe=is_exe,
                                            which=which)
            for extension in ['.1.ebwt', '.2.ebwt', '.3.ebwt', '.4.ebwt', 
                                '.rev.1.ebwt', '.rev.2.ebwt']:
                index_file = bowtie1_idx + extension
                if not ab.Url(index_file).is_local:
                    base_errors.append(('Bowtie 1 index file {0} must be '
                                        'on the local filesystem.').format(
                                            index_file
                                        ))
                elif not os.path.exists(index_file):
                    base.errors.append(('Bowtie 1 index file {0} does not '
                                        'exist.').format(index_file))
            base.bowtie1_idx = bowtie1_idx
            base.bowtie2_exe = base.check_program('bowtie2', 'Bowtie 2',
                                '--bowtie2', entered_exe=bowtie2_exe,
                                is_exe=is_exe, which=which)
            bowtie2_version_command = [base.bowtie2_exe, '--version']
            try:
                base.bowtie2_version = subprocess.check_output(
                        bowtie2_version_command
                    ).split('\n', 1)[0].split(' ')[-1]
            except Exception as e:
                base.errors.append(('Error "{0}" encountered attempting to '
                                    'execute "{1}".').format(
                                                        e.message,
                                                        ' '.join(
                                                        bowtie2_version_command
                                                       )
                                                    ))
            base.bowtie2_build_exe = base.check_program('bowtie2-build',
                                            'Bowtie 2 Build',
                                            '--bowtie2-build',
                                            entered_exe=bowtie2_build_exe,
                                            is_exe=is_exe,
                                            which=which)
            for extension in ['.1.bt2', '.2.bt2', '.3.bt2', '.4.bt2', 
                                '.rev.1.bt2', '.rev.2.bt2']:
                index_file = bowtie2_idx + extension
                if not ab.Url(index_file).is_local:
                    base_errors.append(('Bowtie 2 index file {0} must be '
                                        'on the local filesystem.').format(
                                            index_file
                                        ))
                elif not os.path.exists(index_file):
                    base.errors.append(('Bowtie 2 index file {0} does not '
                                        'exist.').format(index_file))
            base.bowtie2_idx = bowtie2_idx
            base.samtools_exe = base.check_program('samtools', 'SAMTools',
                                '--samtools', entered_exe=samtools_exe,
                                is_exe=is_exe, which=which)
            try:
                samtools_process = subprocess.Popen(
                        [base.samtools_exe], stderr=subprocess.PIPE
                    )
            except Exception as e:
                base.errors.append(('Error "{0}" encountered attempting to '
                                    'execute "{1}".').format(
                                                        e.message,
                                                        base.samtools_exe
                                                    ))
            # Output any errors before detect message is determined
            raise_runtime_error(base)
            base.samtools_version = '<unknown>'
            for line in samtools_process.stderr:
                if 'Version:' in line:
                    base.samtools_version = line.rpartition(' ')[-1].strip()
                    if base.samtools_version[-1] == ')':
                        base.samtools_version = base.samtools_version[:-1]
            base.detect_message =('Detected Bowtie 1 v{0}, Bowtie 2 v{1}, '
                                  'and SAMTools v{2}.').format(
                                               base.bowtie1_version,
                                               base.bowtie2_version,
                                               base.samtools_version
                                            )
            base.bedgraphtobigwig_exe = base.check_program('bedGraphToBigWig', 
                                    'BedGraphToBigWig', '--bedgraphtobigwig',
                                    entered_exe=bedgraphtobigwig_exe,
                                    is_exe=is_exe, which=which)
            # Check input dir
            if input_dir is not None:
                if not os.path.exists(input_dir):
                    base_errors.append(('Input directory (--input) '
                                        '"{0}" does not exist').format(
                                                            input_dir
                                                        ))
                else:
                    base.input_dir = input_dir
        else:
            # Elastic mode; check S3 for genome if necessary
            assert s3_ansible is not None
            if assembly == 'hg19':
                base.index_archive = 's3://rail-emr/index/hg19_UCSC.tar.gz'
            else:
                if not Url(assembly).is_s3:
                    base.errors.append(('Bowtie index archive must be on S3'
                                        ' in "elastic" mode, but '
                                        '"{0}" was entered.').format(assembly))
                elif not s3_ansible.exists(assembly):
                    base.errors.append('Bowtie index archive was not found '
                                       'on S3 at "{0}".'.format(assembly))
                else:
                    base.index_archive = assembly
            if input_dir is not None:
                if not ab.Url(input_dir).is_s3:
                    base.errors.append(('Input directory must be on S3, but '
                                        '"{0}" was entered.').format(
                                                                input_dir
                                                            ))
                elif not s3_ansible.is_dir(input_dir):
                    base.errors.append(('Input directory "{0}" was not found '
                                        'on S3.').format(input_dir))
                else:
                    base.input_dir = input_dir
            # Set up elastic params
            base.bowtie1_idx = _elastic_bowtie1_idx
            base.bowtie2_idx = _elastic_bowtie2_idx
            base.bedgraphtobigwig_exe = _elastic_bedgraphtobigwig_exe
            base.samtools_exe = _elastic_samtools_exe
            base.bowtie1_exe = _elastic_bowtie1_exe
            base.bowtie2_exe = _elastic_bowtie2_exe
            base.bowtie1_build_exe = _elastic_bowtie1_build_exe
            base.bowtie2_build_exe = _elastic_bowtie2_build_exe

        # Assume bowtie2 args are kosher for now
        base.bowtie2_args = bowtie2_args
        if not (isinstance(genome_partition_length, int) and
                genome_partition_length > 0):
            base.errors.append('Genome partition length '
                               '(--genome-partition-length) must be an '
                               'integer > 0, but {0} was entered.'.format(
                                                        genome_partition_length
                                                    ))
        base.genome_partition_length = genome_partition_length
        if not (isinstance(min_readlet_size, int) and min_readlet_size > 0):
            base.errors.append('Minimum readlet size (--min-readlet-size) '
                               'must be an integer > 0, but '
                               '{0} was entered.'.format(min_readlet_size))
        base.min_readlet_size = min_readlet_size
        if not (isinstance(max_readlet_size, int) and max_readlet_size
                >= min_readlet_size):
            base.errors.append('Maximum readlet size (--max-readlet-size) '
                               'must be an integer >= minimum readlet size '
                               '(--min-readlet-size) = '
                               '{0}, but {1} was entered.'.format(
                                                    base.min_readlet_size,
                                                    max_readlet_size
                                                ))
        base.max_readlet_size = max_readlet_size
        if not (isinstance(readlet_config_size, int) and readlet_config_size
                >= max_readlet_size):
            base.errors.append('Readlet config size (--readlet-config-size) '
                               'must be an integer >= maximum readlet size '
                               '(--max-readlet-size) = '
                               '{0}, but {1} was entered.'.format(
                                                    base.max_readlet_size,
                                                    readlet_config_size
                                                ))
        base.readlet_config_size = readlet_config_size
        if not (isinstance(readlet_interval, int) and readlet_interval
                > 0):
            base.errors.append('Readlet interval (--readlet-interval) '
                               'must be an integer > 0, '
                               'but {0} was entered.'.format(
                                                    readlet_interval
                                                ))
        base.readlet_interval = readlet_interval
        if not (cap_size_multiplier > 1):
            base.errors.append('Cap size multiplier (--cap-size-multiplier) '
                               'must be > 1, '
                               'but {0} was entered.'.format(
                                                    cap_size_multiplier
                                                ))
        base.cap_size_multiplier = cap_size_multiplier
        if not (isinstance(min_intron_size, int) and min_intron_size > 0):
            base.errors.append('Minimum intron size (--min-intron-size) '
                               'must be an integer > 0, but '
                               '{0} was entered.'.format(min_intron_size))
        base.min_intron_size = min_intron_size
        if not (isinstance(max_intron_size, int) and max_intron_size
                >= min_intron_size):
            base.errors.append('Maximum intron size (--max-intron-size) '
                               'must be an integer >= minimum intron size '
                               '(--min-readlet-size) = '
                               '{0}, but {1} was entered.'.format(
                                                    base.min_intron_size,
                                                    max_intron_size
                                                ))
        base.max_intron_size = max_intron_size
        if not (isinstance(min_exon_size, int) and min_exon_size > 0):
            base.errors.append('Minimum exon size (--min-exon-size) '
                               'must be an integer > 0, but '
                               '{0} was entered.'.format(min_exon_size))
        base.min_exon_size = min_exon_size
        if search_filter == 'none':
            base.search_filter = 1
        elif search_filter == 'mild':
            base.search_filter = int(base.min_exon_size * 2 / 3)
        elif search_filter == 'strict':
            try:
                base.search_filter = base.min_exon_size
            except:
                pass
        else:
            try:
                base.search_filter = int(search_filter)
            except ValueError:
                # Not an integer
                base.errors.append('Search filter (--search-filter) '
                                   'must be an integer >= 1 or one of '
                                   '{"none", "mild", "strict"}, but {0} was '
                                   'entered.'.format(search_filter))
        if not (isinstance(motif_search_window_size, int) and 
                    motif_search_window_size >= 0):
            base.errors.append('Motif search window size '
                               '(--motif-search-window-size) must be an '
                               'integer >= 0, but {0} was entered.'.format(
                                                    motif_search_window_size
                                                ))
        base.motif_search_window_size = motif_search_window_size
        if max_gaps_mismatches is not None and not (
                isinstance(max_gaps_mismatches, int) and 
                max_gaps_mismatches >= 0
            ):
            base.errors.append('Max gaps and mismatches '
                               '(--max-gaps-mismatches) must be an '
                               'integer >= 0, but {0} was entered.'.format(
                                                    max_gaps_mismatches
                                                ))
        base.max_gaps_mismatches = max_gaps_mismatches
        if not (isinstance(motif_radius, int) and
                    motif_radius >= 0):
            base.errors.append('Motif radius (--motif-radius) must be an '
                               'integer >= 0, but {0} was entered.'.format(
                                                    motif_radius
                                                ))
        base.motif_radius = motif_radius
        base.genome_bowtie1_args = genome_bowtie1_args
        base.transcriptome_bowtie2_args = transcriptome_bowtie2_args
        if not (0 <= normalize_percentile <= 1):
            base.errors.append('Normalization percentile '
                               '(--normalize-percentile) must on the '
                               'interval [0, 1], but {0} was entered'.format(
                                                    normalize_percentile
                                                ))
        base.normalize_percentile = normalize_percentile
        if not (isinstance(tie_margin, int) and
                    tie_margin >= 0):
            base.errors.append('Tie margin (--tie-margin) must be an '
                               'integer >= 0, but {0} was entered.'.format(
                                                    tie_margin
                                                ))
        base.tie_margin = tie_margin
        if not (isinstance(count_multiplier, int) and
                    count_multiplier >= 0):
            base.errors.append('Count multiplier (--count-multiplier) must '
                               'be an integer >= 0, but '
                               '{0} was entered.'.format(
                                                    count_multiplier
                                                ))
        base.count_multiplier = count_multiplier
        confidence_criteria_split = intron_confidence_criteria.split(',')
        confidence_criteria_error = False
        try:
            base.sample_fraction = float(confidence_criteria_split[0])
        except ValueError:
            confidence_criteria_error = True
        else:
            if not (0 <= base.sample_fraction <= 1):
                confidence_criteria_error = True
        try:
            base.coverage_threshold = int(confidence_criteria_split[1])
        except ValueError:
            confidence_criteria_error = True
        else:
            if not (base.coverage_threshold >= 0):
                confidence_criteria_error = True
        if confidence_criteria_error:
            base.errors.append('Intron confidence criteria '
                               '(--intron-confidence-criteria) must be a '
                               'comma-separated list of two elements: the '
                               'first should be a decimal value between 0 '
                               'and 1 inclusive, and the second should be '
                               'an integer >= 1. {0} was entered.'.format(
                                                    intron_confidence_criteria
                                                ))
        base.drop_deletions = drop_deletions
        base.do_not_output_bam_by_chr = do_not_output_bam_by_chr
        if not (isinstance(transcriptome_indexes_per_sample, int)
                    and (1000 >= transcriptome_indexes_per_sample >= 1)):
            base.errors.append('Transcriptome indexes per sample '
                               '(--transcriptome-indexes-per-sample) must be '
                               'an integer between 1 and 1000.')
        base.transcriptome_indexes_per_sample \
            = transcriptome_indexes_per_sample
        base.output_sam = output_sam
        base.bam_basename = bam_basename
        base.bed_basename = bed_basename

    @staticmethod
    def add_args(required_parser, exec_parser, output_parser, algo_parser, 
                    elastic=False):
        """ usage: argparse.SUPPRESS if advanced options should be suppressed;
                else None
        """
        if not elastic:
            exec_parser.add_argument(
                '--bowtie1', type=str, required=False,
                metavar='<exe>',
                default=None,
                help=('path to Bowtie 1 executable (def: bowtie)')
            )
            exec_parser.add_argument(
                '--bowtie1-build', type=str, required=False,
                metavar='<exe>',
                default=None,
                help=('path to Bowtie 1 Build executable (def: bowtie-build)')
            )
            required_parser.add_argument(
                '-1', '--bowtie1-idx', type=str, required=True,
                metavar='<idx>',
                help='path to Bowtie 1 index; include basename'
            )
            exec_parser.add_argument(
                '--bowtie2', type=str, required=False,
                metavar='<exe>',
                default=None,
                help=('path to Bowtie 2 executable (def: bowtie2)')
            )
            exec_parser.add_argument(
                '--bowtie2-build', type=str, required=False,
                metavar='<exe>',
                default=None,
                help=('path to Bowtie 2 Build executable (def: bowtie2-build)')
            )
            required_parser.add_argument(
                '-2', '--bowtie2-idx', type=str, required=True,
                metavar='<idx>',
                help='path to Bowtie 2 index; include basename'
            )
            exec_parser.add_argument(
                '--samtools', type=str, required=False,
                metavar='<exe>',
                default=None,
                help=('path to SAMTools executable (def: samtools)')
            )
            exec_parser.add_argument(
                '--bedgraphtobigwig', type=str, required=False,
                metavar='<exe>',
                default=None,
                help=('path to BedGraphToBigWig executable '
                      '(def: bedGraphToBigWig)')
            )
        else:
            required_parser.add_argument(
                '-a', '--assembly', type=str, required=True,
                metavar='<choice/tgz>',
                help=('assembly to use for alignment. <choice> can be in '
                      '{"hg19"}. otherwise, specify path to tar.gz Rail '
                      'archive on S3')
            )
        algo_parser.add_argument(
                '--bowtie2-args', type=str, required=False,
                default='',
                metavar='<str>',
                help=('arguments to pass to Bowtie 2, which is always run in '
                      '"--local" mode (def: Bowtie 2 defaults)')
            )
        algo_parser.add_argument(
            '--genome-partition-length', type=int, required=False,
            metavar='<int>',
            default=5000,
            help=('smallest unit of genome addressable by single task when '
                  'computing coverage')
        )
        algo_parser.add_argument(
            '--max-readlet-size', type=int, required=False,
            metavar='<int>',
            default=25,
            help='max size of read segment to align when searching for introns'
        )
        algo_parser.add_argument(
            '--readlet-config-size', type=int, required=False,
            metavar='<int>',
            default=35,
            help=('max number of exonic bases spanned by a path enumerated in '
                  'intron DAG')
        )
        algo_parser.add_argument(
            '--min-readlet-size', type=int, required=False,
            metavar='<int>',
            default=15,
            help='min size of read segment to align when searching for introns'
        )
        algo_parser.add_argument(
            '--readlet-interval', type=int, required=False,
            metavar='<int>',
            default=4,
            help=('distance between start positions of successive overlapping '
                  'read segments to align when searching for introns')
        )
        algo_parser.add_argument(
            '--cap-size-multiplier', type=float, required=False,
            default=1.1,
            help=argparse.SUPPRESS
        )
        algo_parser.add_argument(
            '--max-intron-size', type=int, required=False,
            metavar='<int>',
            default=500000,
            help=('filter out introns spanning more than <int> bases')
        )
        algo_parser.add_argument(
            '--min-intron-size', type=int, required=False,
            metavar='<int>',
            default=10,
            help=('filter out introns spanning fewer than <int> bases')
        )
        algo_parser.add_argument(
            '--min-exon-size', type=int, required=False,
            metavar='<int>',
            default=9,
            help=('try to be sensitive to exons that span at least <int> '
                  'bases')
        )
        algo_parser.add_argument(
            '--search-filter', type=str, required=False,
            metavar='<choice/int>',
            default='none',
            help=('filter out reads searched for introns that fall below '
                  'threshold <int> for initially detected anchor length; '
                  'or select <choice> from {"strict", "mild", "none"}')
        )
        algo_parser.add_argument(
            '--intron-confidence-criteria', type=str, required=False,
            metavar='<dec>,<int>',
            default='0.05,5',
            help=('if parameter is "f,c", filter out introns that are not '
                  'either present in at least a fraction f of samples or '
                  'detected in at least c reads of one sample')
        )
        algo_parser.add_argument(
            '--motif-search-window-size', type=int, required=False,
            default=1000,
            help=argparse.SUPPRESS
        )
        algo_parser.add_argument(
            '--max-gaps-mismatches', type=int, required=False,
            default=None,
            help=argparse.SUPPRESS
        )
        algo_parser.add_argument(
            '--motif-radius', type=int, required=False,
            default=5,
            help=argparse.SUPPRESS
        )
        algo_parser.add_argument(
            '--genome-bowtie1-args', type=str, required=False,
            default='-v 0 -a -m 30',
            help=argparse.SUPPRESS
        )
        algo_parser.add_argument(
            '--transcriptome-bowtie2-args', type=str, required=False,
            default='-k 30',
            help=argparse.SUPPRESS
        )
        algo_parser.add_argument(
            '--count-multiplier', type=int, required=False,
            default=15,
            help=argparse.SUPPRESS
        )
        algo_parser.add_argument(
            '--normalize-percentile', type=float, required=False,
            metavar='<dec>',
            default=0.75,
            help=('percentile to use when computing normalization factors for '
                  'sample coverages')
        )
        algo_parser.add_argument(
            '--tie-margin', type=int, required=False,
            metavar='<int>',
            default=0,
            help=('allowed score difference per 100 bases among ties in '
                  'max score. For example, 150 and 144 are tied alignment '
                  'scores for a 100-bp read when --tie-margin is 6')
        )
        algo_parser.add_argument(
            '--transcriptome-indexes-per-sample', type=int,
            metavar='<int>',
            default=50,
            help=argparse.SUPPRESS
        )
        output_parser.add_argument(
            '--drop-deletions', action='store_const', const=True,
            default=False,
            help=('drop deletions from coverage vectors encoded in bigwigs')
        )
        output_parser.add_argument(
            '--do-not-output-bam-by-chr', action='store_const', const=True,
            default=False,
            help=('place all of a sample\'s alignments in one file rather '
                  'than dividing them up by chromosome')
        )
        output_parser.add_argument(
            '--output-sam', action='store_const', const=True,
            default=False,
            help='output SAM instead of BAM'
        )
        output_parser.add_argument(
            '--bam-basename', type=str, required=False,
            metavar='<str>',
            default='alignments',
            help='basename for BAM output'
        )
        output_parser.add_argument(
            '--bed-basename', type=str, required=False,
            metavar='<str>',
            default='',
            help='basename for BED output (def: *empty*)'
        )

    @staticmethod
    def protosteps(base, input_dir, elastic=False):
        manifest = ('/mnt/MANIFEST' if elastic else base.manifest)
        verbose = ('--verbose' if base.verbose else '')
        drop_deletions = ('--drop-deletions' if base.drop_deletions else '')
        keep_alive = ('--keep-alive' if elastic else '')
        scratch  = (('--scratch %s' % base.scratch)
                    if (base.scratch is not None and not elastic) else '')
        return [  
            {
                'name' : 'Align reads and segment them into readlets',
                'run' : ('align_reads.py --bowtie-idx={0} --bowtie2-idx={1} '
                         '--bowtie2-exe={2} '
                         '--exon-differentials --partition-length={3} '
                         '--min-exon-size={4} '
                         '--manifest={5} '
                         '--max-readlet-size={6} '
                         '--readlet-interval={7} '
                         '--capping-multiplier={8} '
                         '--gzip-level {9} '
                         '--index-count {10} '
                         '{11} {12} {13} {14} -- {15}').format(
                                                        base.bowtie1_idx,
                                                        base.bowtie2_idx,
                                                        base.bowtie2_exe,
                                                base.genome_partition_length,
                                                base.search_filter,
                                                        manifest,
                                                        base.max_readlet_size,
                                                        base.readlet_interval,
                                                base.cap_size_multiplier,
                                                base.gzip_level
                                                if 'gzip_level' in
                                                dir(base) else 3,
                                        base.transcriptome_indexes_per_sample *
                                            base.sample_count,
                                                drop_deletions,
                                                        verbose,
                                                        keep_alive,
                                                        scratch,
                                                        base.bowtie2_args),
                'inputs' : [input_dir],
                'no_input_prefix' : True,
                'output' : 'align_reads',
                'min_tasks' : base.sample_count * 12 if elastic else None,
                'part' : 1,
                'keys' : 1,
                'multiple_outputs' : True,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size * 2),
                        'elephantbird.combined.split.count={task_count}'
                    ]
            },
            {
                'name' : 'Align unique readlets to genome',
                'run' : ('align_readlets.py --bowtie-idx={0} '
                         '--bowtie-exe={1} {2} {3} --gzip-level={4} {5} '
                         '-- -t --sam-nohead --startverbose {6}').format(
                                                    base.bowtie1_idx,
                                                    base.bowtie1_exe,
                                                    verbose,
                                                    keep_alive,
                                                    base.gzip_level
                                                    if 'gzip_level' in
                                                    dir(base) else 3,
                                                    scratch,
                                                    base.genome_bowtie1_args,
                                                ),
                'inputs' : [path_join(elastic, 'align_reads', 'readletized')],
                'output' : 'align_readlets',
                'min_tasks' : base.sample_count * 12 if elastic else None,
                'part' : 1,
                'keys' : 1,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size * 2),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Search for introns using readlet alignments',
                'run' : ('intron_search.py --bowtie-idx={0} '
                         '--partition-length={1} --max-intron-size={2} '
                         '--min-intron-size={3} --min-exon-size={4} '
                         '--search-window-size={5} {6} '
                         '--motif-radius={7} {8}').format(
                                                base.bowtie1_idx,
                                                base.genome_partition_length,
                                                base.max_intron_size,
                                                base.min_intron_size,
                                                base.min_exon_size,
                                                base.motif_search_window_size,
                                                ('--max-gaps-mismatches %d' %
                                                 base.max_gaps_mismatches)
                                                if base.max_gaps_mismatches
                                                is not None else '',
                                                base.motif_radius,
                                                verbose
                                            ),
                'inputs' : ['align_readlets'],
                'output' : 'intron_search',
                'min_tasks' : base.sample_count * 12 if elastic else None,
                'part' : 1,
                'keys' : 1,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size * 2),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Filter out introns that violate confidence criteria',
                'run' : ('intron_filter.py --manifest={0} '
                         '--sample-fraction={1} --coverage-threshold={2} '
                         '{3}').format(
                                        manifest,
                                        base.sample_fraction,
                                        base.coverage_threshold,
                                        verbose
                                    ),
                'inputs' : ['intron_search'],
                'output' : 'intron_filter',
                'min_tasks' : (max(base.sample_count / 10, 1)
                                if elastic else None),
                'part' : 3,
                'keys' : 3,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Enumerate possible intron cooccurrences on readlets',
                'run' : ('intron_config.py '
                         '--readlet-size={0} {1}').format(
                                                    base.readlet_config_size,
                                                    verbose
                                                ),
                'inputs' : ['intron_filter'],
                'output' : 'intron_config',
                'taskx' : 1,
                'part' : 2,
                'keys' : 4,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Get transcriptome elements for realignment',
                'run' : ('intron_fasta.py --bowtie-idx={0} {1}').format(
                                                        base.bowtie1_idx,
                                                        verbose
                                                    ),
                'inputs' : ['intron_config'],
                'output' : 'intron_fasta',
                'taskx' : 1,
                'part' : 3,
                'keys' : 3,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Build index of transcriptome elements',
                'run' : ('intron_index.py --bowtie-build-exe={0} '
                         '--out={1} {2} {3}').format(base.bowtie2_build_exe,
                                                 ab.Url(
                                                    path_join(elastic,
                                                        base.output_dir,
                                                        'transcript_index')
                                                    ).to_url(caps=True)
                                                if elastic
                                                else os.path.join(
                                                        base.output_dir,
                                                        'transcript_index'
                                                    ),
                                                 keep_alive, scratch),
                'inputs' : ['intron_fasta'],
                'output' : 'intron_index',
                'min_tasks' : 1,
                'max_tasks' : 1,
                'part' : 1,
                'keys' : 1,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Finalize intron cooccurrences on reads',
                'run' : ('cointron_enum.py --bowtie2-idx={0} --gzip-level {1} '
                         '--bowtie2-exe={2} {3} {4} --intermediate-dir {5} '
                         '{6} -- {7}').format(
                                            'intron/intron'
                                            if elastic else
                                            ab.Url(path_join(elastic,
                                                base.output_dir,
                                                'transcript_index',
                                                'intron')).to_url(),
                                            base.gzip_level
                                            if 'gzip_level' in
                                            dir(base) else 3,
                                            base.bowtie2_exe,
                                            verbose,
                                            keep_alive,
                                            base.intermediate_dir,
                                            scratch,
                                            base.transcriptome_bowtie2_args
                                        ),
                'inputs' : [path_join(elastic, 'align_reads', 'unique')],
                'output' : 'cointron_enum',
                'min_tasks' : base.sample_count * 10 if elastic else None,
                'archives' : ab.Url(path_join(elastic,
                                    base.output_dir,
                                    'transcript_index',
                                    'intron.tar.gz#intron')).to_native_url(),
                'part' : 1,
                'keys' : 1,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size * 2),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Get transcriptome elements for read realignment',
                'run' : ('cointron_fasta.py --bowtie-idx={0} '
                         '--index-count {1} {2}').format(
                                                        base.bowtie1_idx,
                                        base.transcriptome_indexes_per_sample *
                                            base.sample_count,
                                                        verbose
                                                    ),
                'inputs' : ['cointron_enum'],
                'output' : 'cointron_fasta',
                'min_tasks' : base.sample_count * 10 if elastic else None,
                'part' : 3,
                'keys' : 3,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Align reads to transcriptome elements',
                'run' : ('realign_reads.py --bowtie2-exe={0} --gzip-level {1} '
                         '--count-multiplier {2} {3} {4} {5} -- {6}').format(
                                        base.bowtie2_exe,
                                        base.gzip_level
                                        if 'gzip_level' in
                                        dir(base) else 3,
                                        base.count_multiplier,
                                        verbose,
                                        keep_alive,
                                        scratch,
                                        base.bowtie2_args
                                    ),
                'inputs' : [path_join(elastic, 'align_reads', 'unmapped'),
                            'cointron_fasta'],
                'output' : 'realign_reads',
                # Ensure that a single reducer isn't assigned too much fasta
                'min_tasks' : base.sample_count * 12 if elastic else None,
                'part' : 1,
                'keys' : 3,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size * 2),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Collect and compare read alignments',
                'run' : ('compare_alignments.py --bowtie-idx={0} '
                         '--partition-length={1} --exon-differentials '
                         '--tie-margin {2} --manifest={3} '
                         '{4} {5} -- {6}').format(
                                        base.bowtie1_idx,
                                        base.genome_partition_length,
                                        base.tie_margin,
                                        manifest,
                                        drop_deletions,
                                        verbose,
                                        base.bowtie2_args
                                    ),
                'inputs' : [path_join(elastic, 'align_reads', 'postponed_sam'),
                            'realign_reads'],
                'output' : 'compare_alignments',
                'min_tasks' : base.sample_count * 12 if elastic else None,
                'part' : 1,
                'keys' : 1,
                'multiple_outputs' : True,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size * 2),
                        'elephantbird.combined.split.count={task_count}'
                    ]
            },
            {
                'name' : 'Associate spliced alignments with intron coverages',
                'run' : 'intron_coverage.py --bowtie-idx {0}'.format(
                                                        base.bowtie1_idx
                                                    ),
                'inputs' : [path_join(elastic, 'compare_alignments',
                                                    'intron_bed'),
                            path_join(elastic, 'compare_alignments',
                                               'sam_intron_ties')],
                'output' : 'intron_coverage',
                'taskx' : 1,
                'part' : 6,
                'keys' : 7,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Finalize primary alignments of spliced reads',
                'run' : ('break_ties.py --exon-differentials '
                            '--bowtie-idx {0} --partition-length {1} '
                            '--manifest {2} {3} -- {4}').format(
                                    base.bowtie1_idx,
                                    base.genome_partition_length,
                                    manifest,
                                    drop_deletions,
                                    base.bowtie2_args
                                ),
                'inputs' : ['intron_coverage',
                            path_join(elastic, 'compare_alignments',
                                               'sam_clip_ties')],
                'output' : 'break_ties',
                'taskx' : 1,
                'part' : 1,
                'keys' : 1,
                'multiple_outputs' : True,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ]
            },
            {
                'name' : 'Merge exon differentials at same genomic positions',
                'run' : 'sum.py {0}'.format(
                                        keep_alive
                                    ),
                'inputs' : [path_join(elastic, 'align_reads', 'exon_diff'),
                            path_join(elastic, 'compare_alignments',
                                               'exon_diff'),
                            path_join(elastic, 'break_ties', 'exon_diff')],
                'output' : 'collapse',
                'min_tasks' : base.sample_count * 12 if elastic else None,
                'part' : 3,
                'keys' : 3,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Compile sample coverages from exon differentials',
                'run' : ('coverage_pre.py --bowtie-idx={0} '
                         '--partition-stats').format(base.bowtie1_idx),
                'inputs' : ['collapse'],
                'output' : 'precoverage',
                'min_tasks' : base.sample_count * 12 if elastic else None,
                'part' : 2,
                'keys' : 3,
                'multiple_outputs' : True,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ]
            },
            {
                'name' : 'Write bigwigs with exome coverage by sample',
                'run' : ('coverage.py --bowtie-idx={0} --percentile={1} '
                         '--out={2} --bigwig-exe={3} '
                         '--manifest={4} {5} {6}').format(base.bowtie1_idx,
                                                     base.normalize_percentile,
                                                     ab.Url(
                                                        path_join(elastic,
                                                        base.output_dir,
                                                        'coverage_bigwigs')
                                                     ).to_url(caps=True)
                                                     if elastic
                                                     else path_join(elastic,
                                                        base.output_dir,
                                                        'coverage_bigwigs'),
                                                     base.bedgraphtobigwig_exe,
                                                     manifest,
                                                     verbose,
                                                     scratch),
                'inputs' : [path_join(elastic, 'precoverage', 'coverage')],
                'output' : 'coverage',
                'taskx' : 1,
                'part' : 1,
                'keys' : 3,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Write normalization factors for sample coverages',
                'run' : 'coverage_post.py --out={0} --manifest={1} {2}'.format(
                                                        ab.Url(
                                                            path_join(elastic,
                                                            base.output_dir,
                                                    'normalization_factors')
                                                        ).to_url(caps=True)
                                                        if elastic
                                                        else path_join(elastic,
                                                            base.output_dir,
                                                    'normalization_factors'),
                                                        manifest,
                                                        scratch
                                                    ),
                'inputs' : ['coverage'],
                'output' : 'coverage_post',
                'min_tasks' : 1,
                'max_tasks' : 1,
                'part' : 1,
                'keys' : 2,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Aggregate intron and indel results by sample',
                'run' : 'bed_pre.py',
                'inputs' : [path_join(elastic, 'compare_alignments',
                                               'indel_bed'),
                            path_join(elastic, 'break_ties', 'indel_bed'),
                            path_join(elastic, 'compare_alignments',
                                               'intron_bed'),
                            path_join(elastic, 'break_ties', 'intron_bed')],
                'output' : 'prebed',
                'min_tasks' : base.sample_count * 12 if elastic else None,
                'part' : 6,
                'keys' : 6,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Write beds with intron and indel results by sample',
                'run' : ('bed.py --bowtie-idx={0} --out={1} '
                         '--manifest={2} --bed-basename={3} {4}').format(
                                                        base.bowtie1_idx,
                                                        ab.Url(
                                                            path_join(elastic,
                                                            base.output_dir,
                                                        'introns_and_indels')
                                                         ).to_url(caps=True)
                                                        if elastic
                                                        else path_join(elastic,
                                                            base.output_dir,
                                                        'introns_and_indels'),
                                                        manifest,
                                                        base.bed_basename,
                                                        scratch
                                                    ),
                'inputs' : ['prebed'],
                'output' : 'bed',
                'taskx' : 1,
                'part' : 2,
                'keys' : 5,
                'extra_args' : [
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            },
            {
                'name' : 'Write bams with alignments by sample',
                'run' : ('bam.py --out={0} --bowtie-idx={1} '
                         '--samtools-exe={2} --bam-basename={3} '
                         '--manifest={4} {5} {6} {7}').format(
                                        ab.Url(
                                            path_join(elastic,
                                            base.output_dir, 'alignments')
                                        ).to_url(caps=True)
                                        if elastic
                                        else path_join(elastic,
                                            base.output_dir, 'alignments'),
                                        base.bowtie1_idx,
                                        base.samtools_exe,
                                        base.bam_basename,
                                        manifest,
                                        keep_alive,
                                        '--output-by-chromosome'
                                        if not base.do_not_output_bam_by_chr
                                        else '',
                                        scratch
                                    ),
                'inputs' : [path_join(elastic, 'align_reads', 'sam'),
                            path_join(elastic, 'compare_alignments', 'sam'),
                            path_join(elastic, 'break_ties', 'sam')],
                'output' : 'bam',
                'taskx' : 1,
                'part' : (1 if base.do_not_output_bam_by_chr else 2),
                'keys' : 3,
                'extra_args' : [
                        'mapreduce.reduce.shuffle.input.buffer.percent=0.4',
                        'mapreduce.reduce.shuffle.merge.percent=0.4',
                        'elephantbird.use.combine.input.format=true',
                        'elephantbird.combine.split.size=%d'
                            % (_base_combine_split_size),
                        'elephantbird.combined.split.count={task_count}'
                    ],
                'direct_copy' : True
            }]

    @staticmethod
    def bootstrap(base):
        return [
            {
                'Name' : 'Install PyPy',
                'ScriptBootstrapAction' : {
                    'Args' : [],
                    'Path' : 's3://rail-emr/bootstrap/install-pypy.sh'
                }
            },
            {
                'Name' : 'Install Bowtie 1',
                'ScriptBootstrapAction' : {
                    'Args' : [],
                    'Path' : 's3://rail-emr/bootstrap/install-bowtie.sh'
                }
            },
            {
                'Name' : 'Install Bowtie 2',
                'ScriptBootstrapAction' : {
                    'Args' : [],
                    'Path' : 's3://rail-emr/bootstrap/install-bowtie2.sh'
                }
            },
            {
                'Name' : 'Install bedGraphToBigWig',
                'ScriptBootstrapAction' : {
                    'Args' : [
                        '/mnt/bin'
                    ],
                    'Path' : 's3://rail-emr/bootstrap/install-kenttools.sh'
                }
            },
            {
                'Name' : 'Install SAMTools',
                'ScriptBootstrapAction' : {
                    'Args' : [],
                    'Path' : 's3://rail-emr/bootstrap/install-samtools.sh'
                }
            },
            {
                'Name' : 'Install Rail-RNA',
                'ScriptBootstrapAction' : {
                    'Args' : [
                        's3://rail-emr/bin/rail-rna-%s.tar.gz'
                        % version_number,
                        '/mnt'
                    ],
                    'Path' : 's3://rail-emr/bootstrap/install-rail.sh'
                }
            },
            {
                'Name' : 'Transfer Bowtie indexes to nodes',
                'ScriptBootstrapAction' : {
                    'Args' : [
                        '/mnt',
                        base.index_archive
                    ],
                    'Path' : 's3://rail-emr/bootstrap/install-index.sh'
                }
            },
            {
                'Name' : 'Transfer manifest file to nodes',
                'ScriptBootstrapAction' : {
                    'Args' : [
                        base.manifest,
                        '/mnt',
                        'MANIFEST'
                    ],
                    'Path' : 's3://rail-emr/bootstrap/s3cmd_s3.sh'
                }
            }
        ]

class RailRnaLocalPreprocessJson(object):
    """ Constructs JSON for local mode + preprocess job flow. """
    def __init__(self, manifest, output_dir, intermediate_dir='./intermediate',
        force=False, aws_exe=None, profile='default', region='us-east-1',
        verbose=False, nucleotides_per_input=8000000, gzip_input=True,
        num_processes=1, gzip_intermediates=False, gzip_level=3,
        sort_memory_cap=(300*1024), max_task_attempts=4, 
        keep_intermediates=False, check_manifest=True,
        scratch=None, sort_exe=None):
        base = RailRnaErrors(manifest, output_dir, 
            intermediate_dir=intermediate_dir,
            force=force, aws_exe=aws_exe, profile=profile,
            region=region, verbose=verbose)
        RailRnaLocal(base, check_manifest=check_manifest,
            num_processes=num_processes, gzip_intermediates=gzip_intermediates,
            gzip_level=gzip_level, sort_memory_cap=sort_memory_cap,
            max_task_attempts=max_task_attempts,
            keep_intermediates=keep_intermediates, scratch=scratch,
            sort_exe=sort_exe)
        RailRnaPreprocess(base, nucleotides_per_input=nucleotides_per_input,
            gzip_input=gzip_input)
        raise_runtime_error(base)
        self._json_serial = {}
        step_dir = os.path.join(base_path, 'rna', 'steps')
        self._json_serial['Steps'] = steps(RailRnaPreprocess.protosteps(base,
                os.path.join(base.intermediate_dir, 'preprocess'),
                base.output_dir, elastic=False),
                '', '', step_dir,
                base.num_processes,
                base.intermediate_dir, unix=False
            )
        self.base = base
    
    @property
    def json_serial(self):
        return self._json_serial

class RailRnaParallelPreprocessJson(object):
    """ Constructs JSON for parallel mode + preprocess job flow. """
    def __init__(self, manifest, output_dir, intermediate_dir='./intermediate',
        force=False, aws_exe=None, profile='default', region='us-east-1',
        verbose=False, nucleotides_per_input=8000000, gzip_input=True,
        num_processes=1, gzip_intermediates=False, gzip_level=3,
        sort_memory_cap=(300*1024), max_task_attempts=4, ipython_profile=None,
        ipcontroller_json=None, scratch=None, keep_intermediates=False,
        check_manifest=True, sort_exe=None):
        rc = ipython_client(ipython_profile=ipython_profile,
                                ipcontroller_json=ipcontroller_json)
        base = RailRnaErrors(manifest, output_dir, 
            intermediate_dir=intermediate_dir,
            force=force, aws_exe=aws_exe, profile=profile,
            region=region, verbose=verbose)
        RailRnaLocal(base, check_manifest=check_manifest,
            num_processes=len(rc), gzip_intermediates=gzip_intermediates,
            gzip_level=gzip_level, sort_memory_cap=sort_memory_cap,
            max_task_attempts=max_task_attempts,
            keep_intermediates=keep_intermediates, scratch=scratch,
            local=False, parallel=False, sort_exe=sort_exe)
        if ab.Url(base.output_dir).is_local:
            '''Add NFS prefix to ensure tasks first copy files to temp dir and
            subsequently upload to final destination.'''
            base.output_dir = ''.join(['nfs://', os.path.abspath(
                                                        base.output_dir
                                                    )])
        RailRnaPreprocess(base,
            nucleotides_per_input=nucleotides_per_input, gzip_input=gzip_input)
        raise_runtime_error(base)
        ready_engines(rc, base, prep=True)
        engine_bases = {}
        for i in rc.ids:
            engine_bases[i] = RailRnaErrors(
                    manifest, output_dir, intermediate_dir=intermediate_dir,
                    force=force, aws_exe=aws_exe, profile=profile,
                    region=region, verbose=verbose
                )
        apply_async_with_errors(rc, rc.ids, RailRnaLocal, engine_bases,
            check_manifest=check_manifest, num_processes=num_processes,
            gzip_intermediates=gzip_intermediates, gzip_level=gzip_level,
            sort_memory_cap=sort_memory_cap,
            max_task_attempts=max_task_attempts, scratch=scratch,
            keep_intermediates=keep_intermediates, local=False, parallel=True,
            ansible=ab.Ansible(), sort_exe=sort_exe)
        engine_base_checks = {}
        for i in rc.ids:
            engine_base_checks[i] = engine_bases[i].check_program
        if base.check_curl_on_engines:
            apply_async_with_errors(rc, rc.ids, engine_base_checks,
                'curl', 'Curl', '--curl', entered_exe=base.curl_exe,
                reason=base.check_curl_on_engines, is_exe=is_exe, which=which)
        engine_base_checks = {}
        for i in rc.ids:
            engine_base_checks[i] = engine_bases[i].check_s3
        if base.check_s3_on_engines:
            apply_async_with_errors(rc, rc.ids, engine_base_checks,
                reason=base.check_curl_on_engines, is_exe=is_exe, which=which)
        raise_runtime_error(base)
        self._json_serial = {}
        step_dir = os.path.join(base_path, 'rna', 'steps')
        self._json_serial['Steps'] = steps(RailRnaPreprocess.protosteps(base,
                os.path.join(base.intermediate_dir, 'preprocess'),
                base.output_dir, elastic=False),
                '', '', step_dir,
                base.num_processes,
                base.intermediate_dir, unix=False
            )
        self.base = base
    
    @property
    def json_serial(self):
        return self._json_serial

class RailRnaElasticPreprocessJson(object):
    """ Constructs JSON for elastic mode + preprocess job flow. """
    def __init__(self, manifest, output_dir, intermediate_dir='./intermediate',
        force=False, aws_exe=None, profile='default', region='us-east-1',
        verbose=False, nucleotides_per_input=8000000, gzip_input=True,
        log_uri=None, ami_version='3.4.0',
        visible_to_all_users=False, tags='',
        name='Rail-RNA Job Flow',
        action_on_failure='TERMINATE_JOB_FLOW',
        hadoop_jar=None,
        master_instance_count=1, master_instance_type='c1.xlarge',
        master_instance_bid_price=None, core_instance_count=1,
        core_instance_type=None, core_instance_bid_price=None,
        task_instance_count=0, task_instance_type=None,
        task_instance_bid_price=None, ec2_key_name=None, keep_alive=False,
        termination_protected=False, no_consistent_view=False,
        check_manifest=True, intermediate_lifetime=4):
        base = RailRnaErrors(manifest, output_dir, 
            intermediate_dir=intermediate_dir,
            force=force, aws_exe=aws_exe, profile=profile,
            region=region, verbose=verbose)
        RailRnaElastic(base, check_manifest=check_manifest,
            log_uri=log_uri, ami_version=ami_version,
            visible_to_all_users=visible_to_all_users, tags=tags,
            name=name,
            action_on_failure=action_on_failure,
            hadoop_jar=hadoop_jar, master_instance_count=master_instance_count,
            master_instance_type=master_instance_type,
            master_instance_bid_price=master_instance_bid_price,
            core_instance_count=core_instance_count,
            core_instance_type=core_instance_type,
            core_instance_bid_price=core_instance_bid_price,
            task_instance_count=task_instance_count,
            task_instance_type=task_instance_type,
            task_instance_bid_price=task_instance_bid_price,
            ec2_key_name=ec2_key_name, keep_alive=keep_alive,
            termination_protected=termination_protected,
            no_consistent_view=no_consistent_view,
            intermediate_lifetime=intermediate_lifetime)
        RailRnaPreprocess(base, nucleotides_per_input=nucleotides_per_input,
            gzip_input=gzip_input)
        raise_runtime_error(base)
        self._json_serial = {}
        if base.core_instance_count > 0:
            reducer_count = base.core_instance_count \
                * base.instance_core_counts[base.core_instance_type]
        else:
            reducer_count = base.master_instance_count \
                * base.instance_core_counts[base.core_instance_type]
        self._json_serial['Steps'] \
            = RailRnaElastic.hadoop_debugging_steps(base) + steps(
                    RailRnaPreprocess.protosteps(base,
                        path_join(True, base.intermediate_dir, 'preprocess'),
                        base.output_dir, elastic=True),
                    base.action_on_failure,
                    base.hadoop_jar, '/mnt/src/rna/steps',
                    reducer_count, base.intermediate_dir, unix=True,
                    no_consistent_view=base.no_consistent_view
                )
        self._json_serial['AmiVersion'] = base.ami_version
        if base.log_uri is not None:
            self._json_serial['LogUri'] = base.log_uri
        else:
            self._json_serial['LogUri'] = base.output_dir + '.logs'
        self._json_serial['Name'] = base.name
        self._json_serial['NewSupportedProducts'] = []
        self._json_serial['Tags'] = base.tags
        self._json_serial['VisibleToAllUsers'] = (
                'true' if base.visible_to_all_users else 'false'
            )
        self._json_serial['Instances'] = RailRnaElastic.instances(base)
        self._json_serial['BootstrapActions'] \
            = RailRnaPreprocess.bootstrap() \
            + RailRnaElastic.bootstrap(base)
        self.base = base
    
    @property
    def json_serial(self):
        return self._json_serial

class RailRnaLocalAlignJson(object):
    """ Constructs JSON for local mode + align job flow. """
    def __init__(self, manifest, output_dir, input_dir,
        intermediate_dir='./intermediate',
        force=False, aws_exe=None, profile='default', region='us-east-1',
        verbose=False, bowtie1_exe=None,
        bowtie1_idx='genome', bowtie1_build_exe=None, bowtie2_exe=None,
        bowtie2_build_exe=None, bowtie2_idx='genome',
        bowtie2_args='', samtools_exe=None, bedgraphtobigwig_exe=None,
        genome_partition_length=5000, max_readlet_size=25,
        readlet_config_size=32, min_readlet_size=15, readlet_interval=4,
        cap_size_multiplier=1.2, max_intron_size=500000, min_intron_size=10,
        min_exon_size=9, search_filter='none',
        motif_search_window_size=1000, max_gaps_mismatches=3,
        motif_radius=5, genome_bowtie1_args='-v 0 -a -m 80',
        transcriptome_bowtie2_args='-k 30', count_multiplier=15,
        intron_confidence_criteria='0.5,5', tie_margin=6,
        transcriptome_indexes_per_sample=500, normalize_percentile=0.75,
        drop_deletions=False, do_not_output_bam_by_chr=False, output_sam=False,
        bam_basename='alignments', bed_basename='', num_processes=1,
        gzip_intermediates=False, gzip_level=3,
        sort_memory_cap=(300*1024), max_task_attempts=4,
        keep_intermediates=False, scratch=None, sort_exe=None):
        base = RailRnaErrors(manifest, output_dir, 
            intermediate_dir=intermediate_dir,
            force=force, aws_exe=aws_exe, profile=profile,
            region=region, verbose=verbose)
        RailRnaLocal(base, check_manifest=False, num_processes=num_processes,
            gzip_intermediates=gzip_intermediates, gzip_level=gzip_level,
            sort_memory_cap=sort_memory_cap,
            max_task_attempts=max_task_attempts,
            keep_intermediates=keep_intermediates, scratch=scratch,
            sort_exe=sort_exe)
        RailRnaAlign(base, input_dir=input_dir,
            elastic=False, bowtie1_exe=bowtie1_exe,
            bowtie1_idx=bowtie1_idx, bowtie1_build_exe=bowtie1_build_exe,
            bowtie2_exe=bowtie2_exe, bowtie2_build_exe=bowtie2_build_exe,
            bowtie2_idx=bowtie2_idx, bowtie2_args=bowtie2_args,
            samtools_exe=samtools_exe,
            bedgraphtobigwig_exe=bedgraphtobigwig_exe,
            genome_partition_length=genome_partition_length,
            max_readlet_size=max_readlet_size,
            readlet_config_size=readlet_config_size,
            min_readlet_size=min_readlet_size,
            readlet_interval=readlet_interval,
            cap_size_multiplier=cap_size_multiplier,
            max_intron_size=max_intron_size,
            min_intron_size=min_intron_size, min_exon_size=min_exon_size,
            search_filter=search_filter,
            motif_search_window_size=motif_search_window_size,
            max_gaps_mismatches=max_gaps_mismatches,
            motif_radius=motif_radius,
            genome_bowtie1_args=genome_bowtie1_args,
            intron_confidence_criteria=intron_confidence_criteria,
            transcriptome_bowtie2_args=transcriptome_bowtie2_args,
            count_multiplier=count_multiplier,
            tie_margin=tie_margin,
            normalize_percentile=normalize_percentile,
            transcriptome_indexes_per_sample=transcriptome_indexes_per_sample,
            drop_deletions=drop_deletions,
            do_not_output_bam_by_chr=do_not_output_bam_by_chr,
            output_sam=output_sam, bam_basename=bam_basename,
            bed_basename=bed_basename)
        raise_runtime_error(base)
        print_to_screen(base.detect_message)
        self._json_serial = {}
        step_dir = os.path.join(base_path, 'rna', 'steps')
        self._json_serial['Steps'] = steps(RailRnaAlign.protosteps(base,
                base.input_dir, elastic=False), '', '', step_dir,
                base.num_processes, base.intermediate_dir, unix=False
            )
        self.base = base

    @property
    def json_serial(self):
        return self._json_serial

class RailRnaParallelAlignJson(object):
    """ Constructs JSON for local mode + align job flow. """
    def __init__(self, manifest, output_dir, input_dir,
        intermediate_dir='./intermediate',
        force=False, aws_exe=None, profile='default', region='us-east-1',
        verbose=False, bowtie1_exe=None,
        bowtie1_idx='genome', bowtie1_build_exe=None, bowtie2_exe=None,
        bowtie2_build_exe=None, bowtie2_idx='genome',
        bowtie2_args='', samtools_exe=None, bedgraphtobigwig_exe=None,
        genome_partition_length=5000, max_readlet_size=25,
        readlet_config_size=32, min_readlet_size=15, readlet_interval=4,
        cap_size_multiplier=1.2, max_intron_size=500000, min_intron_size=10,
        min_exon_size=9, search_filter='none',
        motif_search_window_size=1000, max_gaps_mismatches=3,
        motif_radius=5, genome_bowtie1_args='-v 0 -a -m 80',
        transcriptome_bowtie2_args='-k 30', count_multiplier=15,
        intron_confidence_criteria='0.5,5', tie_margin=6,
        transcriptome_indexes_per_sample=500, normalize_percentile=0.75,
        drop_deletions=False, do_not_output_bam_by_chr=False, output_sam=False,
        bam_basename='alignments', bed_basename='', num_processes=1,
        ipython_profile=None, ipcontroller_json=None, scratch=None,
        gzip_intermediates=False,
        gzip_level=3, sort_memory_cap=(300*1024), max_task_attempts=4,
        keep_intermediates=False, do_not_copy_index_to_nodes=False,
        sort_exe=None):
        rc = ipython_client(ipython_profile=ipython_profile,
                                ipcontroller_json=ipcontroller_json)
        base = RailRnaErrors(manifest, output_dir, 
            intermediate_dir=intermediate_dir,
            force=force, aws_exe=aws_exe, profile=profile,
            region=region, verbose=verbose)
        RailRnaLocal(base, check_manifest=False,
            num_processes=len(rc), gzip_intermediates=gzip_intermediates,
            gzip_level=gzip_level, sort_memory_cap=sort_memory_cap,
            max_task_attempts=max_task_attempts,
            keep_intermediates=keep_intermediates,
            local=False, parallel=False, scratch=scratch, sort_exe=sort_exe)
        if ab.Url(base.output_dir).is_local:
            '''Add NFS prefix to ensure tasks first copy files to temp dir and
            subsequently upload to S3.'''
            base.output_dir = ''.join(['nfs://', os.path.abspath(
                                                        base.output_dir
                                                    )])
        RailRnaAlign(base, input_dir=input_dir,
            elastic=False, bowtie1_exe=bowtie1_exe,
            bowtie1_idx=bowtie1_idx, bowtie1_build_exe=bowtie1_build_exe,
            bowtie2_exe=bowtie2_exe, bowtie2_build_exe=bowtie2_build_exe,
            bowtie2_idx=bowtie2_idx, bowtie2_args=bowtie2_args,
            samtools_exe=samtools_exe,
            bedgraphtobigwig_exe=bedgraphtobigwig_exe,
            genome_partition_length=genome_partition_length,
            max_readlet_size=max_readlet_size,
            readlet_config_size=readlet_config_size,
            min_readlet_size=min_readlet_size,
            readlet_interval=readlet_interval,
            cap_size_multiplier=cap_size_multiplier,
            max_intron_size=max_intron_size,
            min_intron_size=min_intron_size, min_exon_size=min_exon_size,
            search_filter=search_filter,
            motif_search_window_size=motif_search_window_size,
            max_gaps_mismatches=max_gaps_mismatches,
            motif_radius=motif_radius,
            genome_bowtie1_args=genome_bowtie1_args,
            intron_confidence_criteria=intron_confidence_criteria,
            transcriptome_bowtie2_args=transcriptome_bowtie2_args,
            count_multiplier=count_multiplier,
            tie_margin=tie_margin,
            normalize_percentile=normalize_percentile,
            transcriptome_indexes_per_sample=transcriptome_indexes_per_sample,
            drop_deletions=drop_deletions,
            do_not_output_bam_by_chr=do_not_output_bam_by_chr,
            output_sam=output_sam, bam_basename=bam_basename,
            bed_basename=bed_basename)
        raise_runtime_error(base)
        ready_engines(rc, base, prep=False)
        engine_bases = {}
        for i in rc.ids:
            engine_bases[i] = RailRnaErrors(
                    manifest, output_dir, intermediate_dir=intermediate_dir,
                    force=force, aws_exe=aws_exe, profile=profile,
                    region=region, verbose=verbose
                )
        apply_async_with_errors(rc, rc.ids, RailRnaLocal, engine_bases,
            check_manifest=False, num_processes=num_processes,
            gzip_intermediates=gzip_intermediates, gzip_level=gzip_level,
            sort_memory_cap=sort_memory_cap,
            max_task_attempts=max_task_attempts,
            keep_intermediates=keep_intermediates, local=False, parallel=True,
            ansible=ab.Ansible(), scratch=scratch, sort_exe=sort_exe)
        apply_async_with_errors(rc, rc.ids, RailRnaAlign, engine_bases,
            input_dir=input_dir, elastic=False, bowtie1_exe=bowtie1_exe,
            bowtie1_idx=bowtie1_idx, bowtie1_build_exe=bowtie1_build_exe,
            bowtie2_exe=bowtie2_exe, bowtie2_build_exe=bowtie2_build_exe,
            bowtie2_idx=bowtie2_idx, bowtie2_args=bowtie2_args,
            samtools_exe=samtools_exe,
            bedgraphtobigwig_exe=bedgraphtobigwig_exe,
            genome_partition_length=genome_partition_length,
            max_readlet_size=max_readlet_size,
            readlet_config_size=readlet_config_size,
            min_readlet_size=min_readlet_size,
            readlet_interval=readlet_interval,
            cap_size_multiplier=cap_size_multiplier,
            max_intron_size=max_intron_size,
            min_intron_size=min_intron_size, min_exon_size=min_exon_size,
            search_filter=search_filter,
            motif_search_window_size=motif_search_window_size,
            max_gaps_mismatches=max_gaps_mismatches,
            motif_radius=motif_radius,
            genome_bowtie1_args=genome_bowtie1_args,
            intron_confidence_criteria=intron_confidence_criteria,
            transcriptome_bowtie2_args=transcriptome_bowtie2_args,
            count_multiplier=count_multiplier,
            tie_margin=tie_margin,
            normalize_percentile=normalize_percentile,
            transcriptome_indexes_per_sample=transcriptome_indexes_per_sample,
            drop_deletions=drop_deletions,
            do_not_output_bam_by_chr=do_not_output_bam_by_chr,
            output_sam=output_sam, bam_basename=bam_basename,
            bed_basename=bed_basename)
        engine_base_checks = {}
        for i in rc.ids:
            engine_base_checks[i] = engine_bases[i].check_program
        if base.check_curl_on_engines:
            apply_async_with_errors(rc, rc.ids, engine_base_checks,
                'curl', 'Curl', '--curl', entered_exe=base.curl_exe,
                reason=base.check_curl_on_engines, is_exe=is_exe, which=which)
        engine_base_checks = {}
        for i in rc.ids:
            engine_base_checks[i] = engine_bases[i].check_s3
        if base.check_s3_on_engines:
            apply_async_with_errors(rc, rc.ids, engine_base_checks,
                reason=base.check_curl_on_engines, is_exe=is_exe, which=which)
        raise_runtime_error(engine_bases)
        print_to_screen(base.detect_message)
        self._json_serial = {}
        step_dir = os.path.join(base_path, 'rna', 'steps')
        self._json_serial['Steps'] = steps(RailRnaAlign.protosteps(base,
                base.input_dir, elastic=False), '', '', step_dir,
                base.num_processes, base.intermediate_dir, unix=False
            )
        self.base = base

    @property
    def json_serial(self):
        return self._json_serial

class RailRnaElasticAlignJson(object):
    """ Constructs JSON for elastic mode + align job flow. """
    def __init__(self, manifest, output_dir, input_dir, 
        intermediate_dir='./intermediate',
        force=False, aws_exe=None, profile='default', region='us-east-1',
        verbose=False, bowtie1_exe=None, bowtie1_idx='genome',
        bowtie1_build_exe=None, bowtie2_exe=None,
        bowtie2_build_exe=None, bowtie2_idx='genome',
        bowtie2_args='', samtools_exe=None, bedgraphtobigwig_exe=None,
        genome_partition_length=5000, max_readlet_size=25,
        readlet_config_size=32, min_readlet_size=15, readlet_interval=4,
        cap_size_multiplier=1.2, max_intron_size=500000, min_intron_size=10,
        min_exon_size=9, search_filter='none',
        motif_search_window_size=1000, max_gaps_mismatches=3,
        motif_radius=5, genome_bowtie1_args='-v 0 -a -m 80',
        transcriptome_bowtie2_args='-k 30', count_multiplier=15,
        intron_confidence_criteria='0.5,5', tie_margin=6,
        transcriptome_indexes_per_sample=500, normalize_percentile=0.75,
        drop_deletions=False, do_not_output_bam_by_chr=False,
        output_sam=False, bam_basename='alignments',
        bed_basename='', log_uri=None, ami_version='3.4.0',
        visible_to_all_users=False, tags='',
        name='Rail-RNA Job Flow',
        action_on_failure='TERMINATE_JOB_FLOW',
        hadoop_jar=None,
        master_instance_count=1, master_instance_type='c1.xlarge',
        master_instance_bid_price=None, core_instance_count=1,
        core_instance_type=None, core_instance_bid_price=None,
        task_instance_count=0, task_instance_type=None,
        task_instance_bid_price=None, ec2_key_name=None, keep_alive=False,
        termination_protected=False, no_consistent_view=False,
        intermediate_lifetime=4):
        base = RailRnaErrors(manifest, output_dir, 
            intermediate_dir=intermediate_dir,
            force=force, aws_exe=aws_exe, profile=profile,
            region=region, verbose=verbose)
        RailRnaElastic(base, check_manifest=False,
            log_uri=log_uri, ami_version=ami_version,
            visible_to_all_users=visible_to_all_users, tags=tags,
            name=name, action_on_failure=action_on_failure,
            hadoop_jar=hadoop_jar, master_instance_count=master_instance_count,
            master_instance_type=master_instance_type,
            master_instance_bid_price=master_instance_bid_price,
            core_instance_count=core_instance_count,
            core_instance_type=core_instance_type,
            core_instance_bid_price=core_instance_bid_price,
            task_instance_count=task_instance_count,
            task_instance_type=task_instance_type,
            task_instance_bid_price=task_instance_bid_price,
            ec2_key_name=ec2_key_name, keep_alive=keep_alive,
            termination_protected=termination_protected,
            no_consistent_view=no_consistent_view,
            intermediate_lifetime=intermediate_lifetime)
        RailRnaAlign(base, input_dir=input_dir,
            elastic=True, bowtie1_exe=bowtie1_exe,
            bowtie1_idx=bowtie1_idx, bowtie1_build_exe=bowtie1_build_exe,
            bowtie2_exe=bowtie2_exe, bowtie2_build_exe=bowtie2_build_exe,
            bowtie2_idx=bowtie2_idx, bowtie2_args=bowtie2_args,
            samtools_exe=samtools_exe,
            bedgraphtobigwig_exe=bedgraphtobigwig_exe,
            genome_partition_length=genome_partition_length,
            max_readlet_size=max_readlet_size,
            readlet_config_size=readlet_config_size,
            min_readlet_size=min_readlet_size,
            readlet_interval=readlet_interval,
            cap_size_multiplier=cap_size_multiplier,
            max_intron_size=max_intron_size,
            min_intron_size=min_intron_size, min_exon_size=min_exon_size,
            search_filter=search_filter,
            motif_search_window_size=motif_search_window_size,
            max_gaps_mismatches=max_gaps_mismatches,
            motif_radius=motif_radius,
            genome_bowtie1_args=genome_bowtie1_args,
            intron_confidence_criteria=intron_confidence_criteria,
            transcriptome_bowtie2_args=transcriptome_bowtie2_args,
            count_multiplier=count_multiplier,
            tie_margin=tie_margin,
            normalize_percentile=normalize_percentile,
            transcriptome_indexes_per_sample=transcriptome_indexes_per_sample,
            drop_deletions=drop_deletions,
            do_not_output_bam_by_chr=do_not_output_bam_by_chr,
            output_sam=output_sam, bam_basename=bam_basename,
            bed_basename=bed_basename,
            s3_ansible=ab.S3Ansible(aws_exe=base.aws_exe,
                                        profile=base.profile))
        raise_runtime_error(base)
        self._json_serial = {}
        if base.core_instance_count > 0:
            reducer_count = base.core_instance_count \
                * base.instance_core_counts[base.core_instance_type]
        else:
            reducer_count = base.master_instance_count \
                * base.instance_core_counts[base.core_instance_type]
        self._json_serial['Steps'] \
            = RailRnaElastic.hadoop_debugging_steps(base) + \
                steps(
                    RailRnaAlign.protosteps(base, base.input_dir,
                                                        elastic=True),
                    base.action_on_failure,
                    base.hadoop_jar, '/mnt/src/rna/steps',
                    reducer_count, base.intermediate_dir, unix=True,
                    no_consistent_view=base.no_consistent_view
                )
        self._json_serial['AmiVersion'] = base.ami_version
        if base.log_uri is not None:
            self._json_serial['LogUri'] = base.log_uri
        else:
            self._json_serial['LogUri'] = base.output_dir + '.logs'
        self._json_serial['Name'] = base.name
        self._json_serial['NewSupportedProducts'] = []
        self._json_serial['Tags'] = base.tags
        self._json_serial['VisibleToAllUsers'] = (
                'true' if base.visible_to_all_users else 'false'
            )
        self._json_serial['Instances'] = RailRnaElastic.instances(base)
        self._json_serial['BootstrapActions'] \
            = RailRnaAlign.bootstrap(base) \
            + RailRnaElastic.bootstrap(base)
        self.base = base
    
    @property
    def json_serial(self):
        return self._json_serial

class RailRnaLocalAllJson(object):
    """ Constructs JSON for local mode + preprocess+align job flow. """
    def __init__(self, manifest, output_dir, intermediate_dir='./intermediate',
        force=False, aws_exe=None, profile='default', region='us-east-1',
        verbose=False, nucleotides_per_input=8000000, gzip_input=True,
        bowtie1_exe=None, bowtie1_idx='genome', bowtie1_build_exe=None,
        bowtie2_exe=None, bowtie2_build_exe=None, bowtie2_idx='genome',
        bowtie2_args='', samtools_exe=None, bedgraphtobigwig_exe=None,
        genome_partition_length=5000, max_readlet_size=25,
        readlet_config_size=32, min_readlet_size=15, readlet_interval=4,
        cap_size_multiplier=1.2, max_intron_size=500000, min_intron_size=10,
        min_exon_size=9, search_filter='none',
        motif_search_window_size=1000, max_gaps_mismatches=3,
        motif_radius=5, genome_bowtie1_args='-v 0 -a -m 80',
        intron_confidence_criteria='0.5,5',
        transcriptome_bowtie2_args='-k 30', tie_margin=6, count_multiplier=15,
        transcriptome_indexes_per_sample=500, normalize_percentile=0.75,
        drop_deletions=False, do_not_output_bam_by_chr=False,
        output_sam=False, bam_basename='alignments', bed_basename='',
        num_processes=1, gzip_intermediates=False, gzip_level=3,
        sort_memory_cap=(300*1024), max_task_attempts=4,
        keep_intermediates=False, check_manifest=True, scratch=None,
        sort_exe=None):
        base = RailRnaErrors(manifest, output_dir, 
            intermediate_dir=intermediate_dir,
            force=force, aws_exe=aws_exe, profile=profile,
            region=region, verbose=verbose)
        RailRnaPreprocess(base, nucleotides_per_input=nucleotides_per_input,
            gzip_input=gzip_input)
        RailRnaLocal(base, check_manifest=check_manifest,
            num_processes=num_processes, gzip_intermediates=gzip_intermediates,
            gzip_level=gzip_level, sort_memory_cap=sort_memory_cap,
            max_task_attempts=max_task_attempts,
            keep_intermediates=keep_intermediates, scratch=scratch,
            sort_exe=sort_exe)
        RailRnaAlign(base, bowtie1_exe=bowtie1_exe,
            bowtie1_idx=bowtie1_idx, bowtie1_build_exe=bowtie1_build_exe,
            bowtie2_exe=bowtie2_exe, bowtie2_build_exe=bowtie2_build_exe,
            bowtie2_idx=bowtie2_idx, bowtie2_args=bowtie2_args,
            samtools_exe=samtools_exe,
            bedgraphtobigwig_exe=bedgraphtobigwig_exe,
            genome_partition_length=genome_partition_length,
            max_readlet_size=max_readlet_size,
            readlet_config_size=readlet_config_size,
            min_readlet_size=min_readlet_size,
            readlet_interval=readlet_interval,
            cap_size_multiplier=cap_size_multiplier,
            max_intron_size=max_intron_size,
            min_intron_size=min_intron_size, min_exon_size=min_exon_size,
            search_filter=search_filter,
            motif_search_window_size=motif_search_window_size,
            max_gaps_mismatches=max_gaps_mismatches,
            motif_radius=motif_radius,
            genome_bowtie1_args=genome_bowtie1_args,
            transcriptome_bowtie2_args=transcriptome_bowtie2_args,
            count_multiplier=count_multiplier,
            intron_confidence_criteria=intron_confidence_criteria,
            tie_margin=tie_margin,
            normalize_percentile=normalize_percentile,
            transcriptome_indexes_per_sample=transcriptome_indexes_per_sample,
            drop_deletions=drop_deletions,
            do_not_output_bam_by_chr=do_not_output_bam_by_chr,
            output_sam=output_sam, bam_basename=bam_basename,
            bed_basename=bed_basename)
        raise_runtime_error(base)
        print_to_screen(base.detect_message)
        self._json_serial = {}
        step_dir = os.path.join(base_path, 'rna', 'steps')
        prep_dir = path_join(False, base.intermediate_dir,
                                        'preprocess')
        push_dir = path_join(False, base.intermediate_dir,
                                        'preprocess', 'push')
        self._json_serial['Steps'] = \
            steps(RailRnaPreprocess.protosteps(base,
                prep_dir, push_dir, elastic=False), '', '', step_dir,
                base.num_processes, base.intermediate_dir, unix=False
            ) + \
            steps(RailRnaAlign.protosteps(base,
                push_dir, elastic=False), '', '', step_dir,
                base.num_processes, base.intermediate_dir, unix=False
            )
        self.base = base

    @property
    def json_serial(self):
        return self._json_serial

class RailRnaParallelAllJson(object):
    """ Constructs JSON for local mode + preprocess+align job flow. """
    def __init__(self, manifest, output_dir, intermediate_dir='./intermediate',
        force=False, aws_exe=None, profile='default', region='us-east-1',
        verbose=False, nucleotides_per_input=8000000, gzip_input=True,
        bowtie1_exe=None, bowtie1_idx='genome', bowtie1_build_exe=None,
        bowtie2_exe=None, bowtie2_build_exe=None, bowtie2_idx='genome',
        bowtie2_args='', samtools_exe=None, bedgraphtobigwig_exe=None,
        genome_partition_length=5000, max_readlet_size=25,
        readlet_config_size=32, min_readlet_size=15, readlet_interval=4,
        cap_size_multiplier=1.2, max_intron_size=500000, min_intron_size=10,
        min_exon_size=9, search_filter='none',
        motif_search_window_size=1000, max_gaps_mismatches=3,
        motif_radius=5, genome_bowtie1_args='-v 0 -a -m 80',
        intron_confidence_criteria='0.5,5',
        transcriptome_bowtie2_args='-k 30', tie_margin=6, count_multiplier=15,
        transcriptome_indexes_per_sample=500, normalize_percentile=0.75,
        drop_deletions=False, do_not_output_bam_by_chr=False,
        output_sam=False, bam_basename='alignments', bed_basename='',
        num_processes=1, gzip_intermediates=False, gzip_level=3,
        sort_memory_cap=(300*1024), max_task_attempts=4, ipython_profile=None,
        ipcontroller_json=None, scratch=None, keep_intermediates=False,
        check_manifest=True, do_not_copy_index_to_nodes=False, sort_exe=None):
        rc = ipython_client(ipython_profile=ipython_profile,
                                ipcontroller_json=ipcontroller_json)
        base = RailRnaErrors(manifest, output_dir, 
            intermediate_dir=intermediate_dir,
            force=force, aws_exe=aws_exe, profile=profile,
            region=region, verbose=verbose)
        RailRnaLocal(base, check_manifest=check_manifest,
            num_processes=len(rc), gzip_intermediates=gzip_intermediates,
            gzip_level=gzip_level, sort_memory_cap=sort_memory_cap,
            max_task_attempts=max_task_attempts,
            keep_intermediates=keep_intermediates,
            local=False, parallel=False, scratch=scratch, sort_exe=sort_exe)
        if ab.Url(base.output_dir).is_local:
            '''Add NFS prefix to ensure tasks first copy files to temp dir and
            subsequently upload to S3.'''
            base.output_dir = ''.join(['nfs://', os.path.abspath(
                                                        base.output_dir
                                                    )])
        RailRnaPreprocess(base, nucleotides_per_input=nucleotides_per_input,
            gzip_input=gzip_input)
        RailRnaAlign(base, bowtie1_exe=bowtie1_exe,
            bowtie1_idx=bowtie1_idx, bowtie1_build_exe=bowtie1_build_exe,
            bowtie2_exe=bowtie2_exe, bowtie2_build_exe=bowtie2_build_exe,
            bowtie2_idx=bowtie2_idx, bowtie2_args=bowtie2_args,
            samtools_exe=samtools_exe,
            bedgraphtobigwig_exe=bedgraphtobigwig_exe,
            genome_partition_length=genome_partition_length,
            max_readlet_size=max_readlet_size,
            readlet_config_size=readlet_config_size,
            min_readlet_size=min_readlet_size,
            readlet_interval=readlet_interval,
            cap_size_multiplier=cap_size_multiplier,
            max_intron_size=max_intron_size,
            min_intron_size=min_intron_size, min_exon_size=min_exon_size,
            search_filter=search_filter,
            motif_search_window_size=motif_search_window_size,
            max_gaps_mismatches=max_gaps_mismatches,
            motif_radius=motif_radius,
            genome_bowtie1_args=genome_bowtie1_args,
            transcriptome_bowtie2_args=transcriptome_bowtie2_args,
            count_multiplier=count_multiplier,
            intron_confidence_criteria=intron_confidence_criteria,
            tie_margin=tie_margin,
            normalize_percentile=normalize_percentile,
            transcriptome_indexes_per_sample=transcriptome_indexes_per_sample,
            drop_deletions=drop_deletions,
            do_not_output_bam_by_chr=do_not_output_bam_by_chr,
            output_sam=output_sam, bam_basename=bam_basename,
            bed_basename=bed_basename)
        raise_runtime_error(base)
        ready_engines(rc, base, prep=False)
        engine_bases = {}
        for i in rc.ids:
            engine_bases[i] = RailRnaErrors(
                    manifest, output_dir, intermediate_dir=intermediate_dir,
                    force=force, aws_exe=aws_exe, profile=profile,
                    region=region, verbose=verbose
                )
        apply_async_with_errors(rc, rc.ids, RailRnaLocal, engine_bases,
            check_manifest=check_manifest, num_processes=num_processes,
            gzip_intermediates=gzip_intermediates, gzip_level=gzip_level,
            sort_memory_cap=sort_memory_cap,
            max_task_attempts=max_task_attempts,
            keep_intermediates=keep_intermediates, local=False, parallel=True,
            ansible=ab.Ansible(), scratch=scratch, sort_exe=sort_exe)
        apply_async_with_errors(rc, rc.ids, RailRnaAlign, engine_bases,
            bowtie1_exe=bowtie1_exe,
            bowtie1_idx=bowtie1_idx, bowtie1_build_exe=bowtie1_build_exe,
            bowtie2_exe=bowtie2_exe, bowtie2_build_exe=bowtie2_build_exe,
            bowtie2_idx=bowtie2_idx, bowtie2_args=bowtie2_args,
            samtools_exe=samtools_exe,
            bedgraphtobigwig_exe=bedgraphtobigwig_exe,
            genome_partition_length=genome_partition_length,
            max_readlet_size=max_readlet_size,
            readlet_config_size=readlet_config_size,
            min_readlet_size=min_readlet_size,
            readlet_interval=readlet_interval,
            cap_size_multiplier=cap_size_multiplier,
            max_intron_size=max_intron_size,
            min_intron_size=min_intron_size, min_exon_size=min_exon_size,
            search_filter=search_filter,
            motif_search_window_size=motif_search_window_size,
            max_gaps_mismatches=max_gaps_mismatches,
            motif_radius=motif_radius,
            genome_bowtie1_args=genome_bowtie1_args,
            transcriptome_bowtie2_args=transcriptome_bowtie2_args,
            count_multiplier=count_multiplier,
            intron_confidence_criteria=intron_confidence_criteria,
            tie_margin=tie_margin,
            normalize_percentile=normalize_percentile,
            transcriptome_indexes_per_sample=transcriptome_indexes_per_sample,
            drop_deletions=drop_deletions,
            do_not_output_bam_by_chr=do_not_output_bam_by_chr,
            output_sam=output_sam, bam_basename=bam_basename,
            bed_basename=bed_basename)
        engine_base_checks = {}
        for i in rc.ids:
            engine_base_checks[i] = engine_bases[i].check_program
        if base.check_curl_on_engines:
            apply_async_with_errors(rc, rc.ids, engine_base_checks,
                'curl', 'Curl', '--curl', entered_exe=base.curl_exe,
                reason=base.check_curl_on_engines, is_exe=is_exe, which=which)
        engine_base_checks = {}
        for i in rc.ids:
            engine_base_checks[i] = engine_bases[i].check_s3
        if base.check_s3_on_engines:
            apply_async_with_errors(rc, rc.ids, engine_base_checks,
                reason=base.check_curl_on_engines, is_exe=is_exe, which=which)
        raise_runtime_error(engine_bases)
        print_to_screen(base.detect_message)
        self._json_serial = {}
        step_dir = os.path.join(base_path, 'rna', 'steps')
        prep_dir = path_join(False, base.intermediate_dir,
                                        'preprocess')
        push_dir = path_join(False, base.intermediate_dir,
                                        'preprocess', 'push')
        self._json_serial['Steps'] = \
            steps(RailRnaPreprocess.protosteps(base,
                prep_dir, push_dir, elastic=False), '', '', step_dir,
                base.num_processes, base.intermediate_dir, unix=False
            ) + \
            steps(RailRnaAlign.protosteps(base,
                push_dir, elastic=False), '', '', step_dir,
                base.num_processes, base.intermediate_dir, unix=False
            )
        self.base = base

    @property
    def json_serial(self):
        return self._json_serial

class RailRnaElasticAllJson(object):
    """ Constructs JSON for elastic mode + preprocess+align job flow. """
    def __init__(self, manifest, output_dir, intermediate_dir='./intermediate',
        force=False, aws_exe=None, profile='default', region='us-east-1',
        verbose=False, nucleotides_per_input=8000000, gzip_input=True,
        bowtie1_exe=None, bowtie1_idx='genome', bowtie1_build_exe=None,
        bowtie2_exe=None, bowtie2_build_exe=None, bowtie2_idx='genome',
        bowtie2_args='', samtools_exe=None, bedgraphtobigwig_exe=None,
        genome_partition_length=5000, max_readlet_size=25,
        readlet_config_size=32, min_readlet_size=15, readlet_interval=4,
        cap_size_multiplier=1.2, max_intron_size=500000, min_intron_size=10,
        min_exon_size=9, search_filter='none',
        motif_search_window_size=1000, max_gaps_mismatches=3,
        motif_radius=5, genome_bowtie1_args='-v 0 -a -m 80',
        transcriptome_bowtie2_args='-k 30', tie_margin=6, count_multiplier=15,
        intron_confidence_criteria='0.5,5', normalize_percentile=0.75,
        transcriptome_indexes_per_sample=500, drop_deletions=False,
        do_not_output_bam_by_chr=False, output_sam=False,
        bam_basename='alignments', bed_basename='',
        log_uri=None, ami_version='3.4.0',
        visible_to_all_users=False, tags='',
        name='Rail-RNA Job Flow',
        action_on_failure='TERMINATE_JOB_FLOW',
        hadoop_jar=None,
        master_instance_count=1, master_instance_type='c1.xlarge',
        master_instance_bid_price=None, core_instance_count=1,
        core_instance_type=None, core_instance_bid_price=None,
        task_instance_count=0, task_instance_type=None,
        task_instance_bid_price=None, ec2_key_name=None, keep_alive=False,
        termination_protected=False, check_manifest=True,
        no_consistent_view=False, intermediate_lifetime=4):
        base = RailRnaErrors(manifest, output_dir, 
            intermediate_dir=intermediate_dir,
            force=force, aws_exe=aws_exe, profile=profile,
            region=region, verbose=verbose)
        RailRnaElastic(base, check_manifest=check_manifest, 
            log_uri=log_uri, ami_version=ami_version,
            visible_to_all_users=visible_to_all_users, tags=tags,
            name=name,
            action_on_failure=action_on_failure,
            hadoop_jar=hadoop_jar, master_instance_count=master_instance_count,
            master_instance_type=master_instance_type,
            master_instance_bid_price=master_instance_bid_price,
            core_instance_count=core_instance_count,
            core_instance_type=core_instance_type,
            core_instance_bid_price=core_instance_bid_price,
            task_instance_count=task_instance_count,
            task_instance_type=task_instance_type,
            task_instance_bid_price=task_instance_bid_price,
            ec2_key_name=ec2_key_name, keep_alive=keep_alive,
            termination_protected=termination_protected,
            no_consistent_view=no_consistent_view,
            intermediate_lifetime=intermediate_lifetime)
        RailRnaPreprocess(base, nucleotides_per_input=nucleotides_per_input,
            gzip_input=gzip_input)
        RailRnaAlign(base, elastic=True, bowtie1_exe=bowtie1_exe,
            bowtie1_idx=bowtie1_idx, bowtie1_build_exe=bowtie1_build_exe,
            bowtie2_exe=bowtie2_exe, bowtie2_build_exe=bowtie2_build_exe,
            bowtie2_idx=bowtie2_idx, bowtie2_args=bowtie2_args,
            samtools_exe=samtools_exe,
            bedgraphtobigwig_exe=bedgraphtobigwig_exe,
            genome_partition_length=genome_partition_length,
            max_readlet_size=max_readlet_size,
            readlet_config_size=readlet_config_size,
            min_readlet_size=min_readlet_size,
            readlet_interval=readlet_interval,
            cap_size_multiplier=cap_size_multiplier,
            max_intron_size=max_intron_size,
            min_intron_size=min_intron_size,
            search_filter=search_filter,
            min_exon_size=min_exon_size,
            motif_search_window_size=motif_search_window_size,
            max_gaps_mismatches=max_gaps_mismatches,
            motif_radius=motif_radius,
            genome_bowtie1_args=genome_bowtie1_args,
            transcriptome_bowtie2_args=transcriptome_bowtie2_args,
            count_multiplier=count_multiplier,
            intron_confidence_criteria=intron_confidence_criteria,
            tie_margin=tie_margin,
            normalize_percentile=normalize_percentile,
            transcriptome_indexes_per_sample=transcriptome_indexes_per_sample,
            drop_deletions=drop_deletions,
            do_not_output_bam_by_chr=do_not_output_bam_by_chr,
            output_sam=output_sam, bam_basename=bam_basename,
            bed_basename=bed_basename,
            s3_ansible=ab.S3Ansible(aws_exe=base.aws_exe,
                                        profile=base.profile))
        raise_runtime_error(base)
        self._json_serial = {}
        if base.core_instance_count > 0:
            reducer_count = base.core_instance_count \
                * base.instance_core_counts[base.core_instance_type]
        else:
            reducer_count = base.master_instance_count \
                * base.instance_core_counts[base.core_instance_type]
        prep_dir = path_join(True, base.intermediate_dir,
                                        'preprocess')
        push_dir = path_join(True, base.intermediate_dir,
                                        'preprocess', 'push')
        self._json_serial['Steps'] \
            = RailRnaElastic.hadoop_debugging_steps(base) + \
                steps(
                    RailRnaPreprocess.protosteps(base, prep_dir, push_dir,
                                                    elastic=True),
                    base.action_on_failure,
                    base.hadoop_jar, '/mnt/src/rna/steps',
                    reducer_count, base.intermediate_dir, unix=True,
                    no_consistent_view=base.no_consistent_view
                ) + \
                steps(
                    RailRnaAlign.protosteps(base, push_dir, elastic=True),
                    base.action_on_failure,
                    base.hadoop_jar, '/mnt/src/rna/steps',
                    reducer_count, base.intermediate_dir, unix=True,
                    no_consistent_view=base.no_consistent_view
                )
        self._json_serial['AmiVersion'] = base.ami_version
        if base.log_uri is not None:
            self._json_serial['LogUri'] = base.log_uri
        else:
            self._json_serial['LogUri'] = base.output_dir + '.logs'
        self._json_serial['Name'] = base.name
        self._json_serial['NewSupportedProducts'] = []
        self._json_serial['Tags'] = base.tags
        self._json_serial['VisibleToAllUsers'] = (
                'true' if base.visible_to_all_users else 'false'
            )
        self._json_serial['Instances'] = RailRnaElastic.instances(base)
        self._json_serial['BootstrapActions'] \
            = RailRnaAlign.bootstrap(base) \
            + RailRnaElastic.bootstrap(base)
        self.base = base
    
    @property
    def json_serial(self):
        return self._json_serial
