from rez import __version__, module_root_path
from rez.config import Resolver
from rez.system import system
from rez.settings import settings
from rez.util import columnise, convert_old_commands, shlex_join, \
    mkdtemp_, rmdtemp, print_warning_once
from rez.rex import RexExecutor, Python
from rez.shells import create_shell, get_shell_types
import pickle
import getpass
import inspect
import time
import sys
import os
import os.path



class ResolvedContext(object):
    """
    The main Rez entry point for creating, saving, loading and executing
    resolved environments. A ResolvedContext object can be saved to file and
    loaded at a later date, and it can reconstruct the equivalent environment
    at that time. It can spawn interactive and non-interactive shells, in any
    supported shell plugin type, such as bash and tcsh. It can also run a
    command within a configured python namespace, without spawning a child
    shell.
    """

    # This must be updated when the ResolvedContext class, or any class used by
    # it, changes. A minor version update means that data has been added, but it
    # can still be read by an earlier Rez version, and a major version update
    # means that backwards compatibility has been broken.
    serialize_version = (1,0)

    def __init__(self, \
        requested_packages,
        resolve_mode='latest',
        quiet=False,
        verbosity=0,
        max_fails=-1,
        timestamp=0,
        build_requires=False,
        assume_dt=True,
        caching=True,
        package_paths=None,
        add_implicit_packages=True):
        """
        Perform a package resolve, and store the result.
        @param requested_packages List of package strings defining the request,
            for example ['boost-1.43+', 'python-2.6']
        @param resolve_mode One of: 'earliest', 'latest'
        @param quiet If True then hides unnecessary output
        @param verbosity Print extra debugging info. One of: 0..2
        @param max_fails Return after N failed configuration attempts
        @param timestamp Ignore packages newer than this time-date.
        @param assume_dt Assume dependency transitivity
        @param caching If True, resolve info is read from and written to a
            memcache daemon, if possible.
        @param package_paths List of paths to search for pkgs, defaults to
            settings.packages_path.
        @param add_implicit_packages If True, the implicit package list
            defined by settings.implicit_packages is added to the request.
        """
        # serialization version
        self.serialize_ver = self.serialize_version

        # resolving settings
        self.req_packages = requested_packages
        self.resolve_mode = resolve_mode
        self.request_time = timestamp
        self.build_requires = build_requires
        self.assume_dt = assume_dt
        self.caching = caching
        self.package_paths = package_paths
        self.add_implicit_packages = add_implicit_packages

        # info about env the resolve occurred in, useful for debugging
        self.user = getpass.getuser()
        self.host = system.fqdn
        self.platform = system.platform
        self.arch = system.arch
        self.os = system.os
        self.shell = system.shell
        self.rez_version = __version__
        self.rez_path = module_root_path
        self.implicit_packages = settings.implicit_packages
        self.created = int(time.time())

        # do the resolve
        resolver = Resolver( \
            resolve_mode=resolve_mode,
            quiet=quiet,
            verbosity=verbosity,
            max_fails=max_fails,
            time_epoch=timestamp,
            build_requires=build_requires,
            assume_dt=assume_dt,
            caching=caching,
            package_paths=package_paths)

        self.result = resolver.resolve(self.req_packages, \
            no_os=(not self.add_implicit_packages),
            meta_vars=['tools'],
            shallow_meta_vars=['tools'])

    @property
    def requested_packages(self):
        """ str list of initially requested packages, not including implicit
        packages """
        return self.req_packages

    @property
    def added_implicit_packages(self):
        """ str list of packages implicitly added to the request list """
        return self.implicit_packages if self.add_implicit_packages else []

    @property
    def resolved_packages(self):
        """ list of `ResolvedPackage` objects representing the resolve """
        return self.result.package_resolves

    @property
    def resolve_graph(self):
        """ dot-graph string representing the resolve process """
        return self.result.dot_graph

    def save(self, path):
        """
        Save the resolved context to file.
        """
        with open(path, 'w') as f:
            pickle.dump(self, f)

    @staticmethod
    def load(path):
        """
        Load a resolved context from file.
        """
        def _v(t):
            return '%d.%d' % t

        curr_ver = ResolvedContext.serialize_version
        with open(path) as f:
            r = pickle.load(f)

        if r.serialize_ver < curr_ver:
            raise Exception("The version of the context (v%s) is too old, "
                "must be v%s or greater" % (_v(r.serialize_ver), _v(curr_ver)))
        if r.serialize_ver[0] > curr_ver[0]:
            next_major = (curr_ver[0]+1, 0)
            raise Exception("The version of the context (v%s) is too new - "
                "this version of Rez can only read contexts earlier than v%s" \
                % (_v(r.serialize_ver), _v(next_major)))
        return r

    def validate(self):
        """
        Check the context against the current system to see if they are
        compatible. For instance, a loaded context may have been created on a
        different host, with different package search paths, and so may refer
        to packages not available on the current host.
        """
        # check package paths
        for pkg in self.result.package_resolves:
            if not os.path.exists(pkg.root):
                raise Exception("Package %s path does not exist: %s" \
                    % (pkg.short_name(), pkg.root))

        # check system packages
        # FIXME TODO

    def print_info(self, buf=sys.stdout, verbose=False):
        """
        Prints a message summarising the contents of the resolved context.
        """
        def _pr(s=''):
            print >> buf, s

        def _rt(t):
            if verbose:
                s = time.strftime("%a %b %d %H:%M:%S %Z %Y", time.localtime(t))
                return s + " (%d)" % int(t)
            else:
                return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(t))

        t_str = _rt(self.created)
        _pr("resolved by %s@%s, on %s, using Rez v%s" \
            % (self.user, self.host, t_str, self.rez_version))
        if self.request_time:
            t_str = _rt(self.request_time)
            _pr("packages released after %s are being ignored" % t_str)
        _pr()

        if verbose:
            _pr("search paths:")
            for path in settings.packages_path:
                _pr(path)
            _pr()

        if self.add_implicit_packages and self.implicit_packages:
            _pr("implicit packages:")
            for pkg in self.implicit_packages:
                _pr(pkg)
            _pr()

        _pr("requested packages:")
        for pkg in self.req_packages:
            _pr(pkg)
        _pr()

        _pr("resolved packages:")
        rows = []
        for pkg in self.result.package_resolves:
            tok = ''
            if not os.path.exists(pkg.root):
                tok = 'NOT FOUND'
            elif pkg.root.startswith(settings.local_packages_path):
                tok = 'local'
            rows.append((pkg.short_name(), pkg.root, tok))
        _pr('\n'.join(columnise(rows)))

    def get_environ(self, parent_environ=None):
        """
        Get the environ dict resulting from interpreting this context.
        @param parent_environ Environment to interpret the context within,
            defaults to os.environ if None.
        @returns The environment dict generated by this context, when
            interpreted in a python rex interpreter.
        """
        interp = Python(target_environ={}, passive=True)
        executor = RexExecutor(interpreter=interp, parent_environ=parent_environ)
        self._execute(executor)
        return executor.get_output()

    def get_shell_code(self, shell=None, parent_environ=None):
        """
        Get the shell code resulting from intepreting this context.
        @param shell Shell type, for eg 'bash'. If None, the current shell type
            is used.
        @param parent_environ Environment to interpret the context within,
            defaults to os.environ if None.
        """
        from rez.shells import create_shell
        sh = create_shell(shell)

        executor = RexExecutor(interpreter=sh, parent_environ=parent_environ)
        self._execute(executor)
        return executor.get_output()

    def apply(self, parent_environ=None):
        """
        Apply the context to the current python session - this updates os.environ
        and possibly sys.path.
        @param environ Environment to interpret the context within, defaults to
            os.environ if None.
        """
        interpreter = Python(target_environ=os.environ)
        executor = RexExecutor(interpreter=interpreter, parent_environ=parent_environ)
        self._execute(executor)

    def execute_command(self, args, parent_environ=None, **subprocess_kwargs):
        """
        Run a command within a resolved context. This only creates the context
        within python - to execute within a full context (so that aliases are
        set, for example) use execute_shell.
        @param args Command arguments, can be a string.
        @param parent_environ Environment to interpret the context within,
            defaults to os.environ if None.
        @param subprocess_kwargs Args to pass to subprocess.Popen.
        @returns a subprocess.Popen object.
        @note This does not alter the current python session.
        """
        interpreter = Python(target_environ={})
        executor = RexExecutor(interpreter=interpreter, parent_environ=parent_environ)
        self._execute(executor)
        return interpreter.subprocess(args, **subprocess_kwargs)

    def execute_shell(self, shell=None, parent_environ=None, rcfile=None,
                      norc=False, stdin=False, command=None, quiet=False,
                      block=None, **Popen_args):
        """
        Spawn a possibly-interactive shell.
        @param shell Shell type, for eg 'bash'. If None, the current shell type
            is used.
        @param parent_environ Environment to interpret the context within,
            defaults to os.environ if None.
        @param rcfile Specify a file to source instead of shell startup files.
        @param norc If True, skip shell startup files, if possible.
        @param stdin If True, read commands from stdin, in a non-interactive shell.
        @param command If not None, execute this command in a non-interactive
            shell. Can be a list of args.
        @param quiet If True, skip the welcome message in interactive shells.
        @param popen_args args to pass to the shell process object constructor.
        @returns A subprocess.Popen object representing the shell process.
        """
        if hasattr(command, "__iter__"):
            command = shlex_join(command)

        # block if the shell is likely to be interactive
        if block is None:
            block = not (command or stdin)

        # create the shell
        from rez.shells import create_shell
        sh = create_shell(shell)

        # context and rxt files
        tmpdir = mkdtemp_()
        context_file = os.path.join(tmpdir, "context.%s" % sh.file_extension())
        rxt_file = os.path.join(tmpdir, "context.rxt")
        self.save(rxt_file)

        # interpret this context and write out the native context file
        executor = RexExecutor(interpreter=sh, parent_environ=parent_environ)
        executor.env.REZ_RXT_FILE = rxt_file
        executor.env.REZ_CONTEXT_FILE = context_file
        self._execute(executor)
        context_code = executor.get_output()
        with open(context_file, 'w') as f:
            f.write(context_code)

        # spawn the shell subprocess
        p = sh.spawn_shell(context_file,
                           rcfile=rcfile,
                           norc=norc,
                           stdin=stdin,
                           command=command,
                           quiet=quiet,
                           **Popen_args)
        if block:
            stdout,stderr = p.communicate()
            return p.returncode,stdout,stderr
        else:
            return p

    def save_resolve_graph(self, path, fmt=None, image_ratio=None,
                           prune_to_package=None):
        """
        Write the resolve graph to an image or dot file.
        @param path File to write to.
        @param fmt File format, determined from path ext if None.
        @param image_ratio Image height / image width.
        @param prune_to_package Only display nodes dependent (directly or
            indirectly) on the given package (str).
        """
        from rez.dot import save_graph
        save_graph(self.resolve_graph, path,
                   fmt=fmt,
                   image_ratio=image_ratio,
                   prune_to_package=prune_to_package)

    def _get_shell_code(self, shell, parent_environ):
        # create the shell
        from rez.shells import create_shell
        sh = create_shell(shell)

        # interpret this context and write out the native context file
        executor = RexExecutor(interpreter=sh, parent_environ=parent_environ)
        self._execute(executor)
        context_code = executor.get_output()

        return sh,context_code

    def _execute(self, executor):
        def _stringify_pkgs(pkgs):
            return ' '.join(x.short_name() for x in pkgs)

        executor.update_env({
            "REZ_USED":             self.rez_path,
            # TODO add back if and when we need this
            #"REZ_PREV_REQUEST":     "$REZ_REQUEST",
            # TODO if we do this when we need to do for all possible settings in evars...
            #"REZ_PACKAGES_PATH":    "$REZ_PACKAGES_PATH",
            "REZ_REQUEST":          _stringify_pkgs(self.result.package_requests),
            "REZ_RAW_REQUEST":      _stringify_pkgs(self.result.raw_package_requests),
            "REZ_RESOLVE":          _stringify_pkgs(self.result.package_resolves),
            "REZ_RESOLVE_MODE":     self.result.resolve_mode,
            "REZ_FAILED_ATTEMPTS":  self.result.failed_attempts,
            "REZ_REQUEST_TIME":     self.result.request_timestamp})

        executor.bind('building', bool(os.getenv('REZ_BUILD_ENV')))

        manager = executor.manager

        # TODO set metavars, shallow_metavars
        for pkg_res in self.result.package_resolves:
            manager.comment("")
            manager.comment("Commands from package %s" % pkg_res.short_name())
            manager.comment("")

            prefix = "REZ_" + pkg_res.name.upper()
            executor.update_env({
                prefix+"_VERSION":  pkg_res.version,
                prefix+"_BASE":     pkg_res.base,
                prefix+"_ROOT":     pkg_res.root})

            executor.bind('this', pkg_res)
            executor.bind('root', pkg_res.root)
            executor.bind('base', pkg_res.base)
            executor.bind('version', pkg_res.version)

            commands = pkg_res.metadata.get("commands")
            if commands:
                # old-style, we convert it to a rex code string (ie python)
                if isinstance(commands, list):
                    if settings.warn_old_commands:
                        print_warning_once("%s is using old-style commands."
                                           % pkg_res.short_name())

                    # convert expansions from !OLD! style to {new}
                    cmds = []
                    for cmd in commands:
                        cmd = cmd.replace("!VERSION!",      "{version}")
                        cmd = cmd.replace("!MAJOR_VERSION!","{version.major}")
                        cmd = cmd.replace("!MINOR_VERSION!","{version.minor}")
                        cmd = cmd.replace("!BASE!",         "{base}")
                        cmd = cmd.replace("!ROOT!",         "{root}")
                        cmd = cmd.replace("!USER!",         "{user}")
                        cmds.append(cmd)
                    commands = convert_old_commands(cmds)

                try:
                    if isinstance(commands, basestring):
                        # rex code in a string
                        executor.execute_code(commands, pkg_res.metafile)
                    elif inspect.isfunction(commands):
                        # function in a package.py
                        executor.execute_function(commands)
                except Exception as e:
                    msg = "Error in commands in file %s:\n%s" \
                          % (pkg_res.metafile, str(e))
                    raise PkgCommandError(msg)