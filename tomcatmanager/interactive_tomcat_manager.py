#
# -*- coding: utf-8 -*-
#
# Copyright (c) 2007 Jared Crapo
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
"""
Classes and functions for the 'tomcat-manager' command line program.
"""

import sys
import os
import traceback
import getpass
import xml.dom.minidom
import ast
import configparser
import shlex
from http.client import responses
import argparse

from attrdict import AttrDict
import cmd2
import requests
import appdirs

import tomcatmanager as tm


def requires_connection(func):
    """Decorator for interactive methods which require a connection."""
    def _requires_connection(self, *args, **kwargs):
        if self.tomcat and self.tomcat.is_connected:
            func(self, *args, **kwargs)
        else:
            # print the message
            self.exit_code = self.exit_codes.error
            self.perror('not connected')
    _requires_connection.__doc__ = func.__doc__
    return _requires_connection

def _path_version_parser(cmdname, helpmsg):
    """Construct an argparser using the given parameters"""
    parser = argparse.ArgumentParser(prog=cmdname, description=helpmsg)
    parser.add_argument('-v', '--version', help='Optional version string of the application to {cmdname}. If the application was deployed with a version string, it must be specified in order to {cmdname} the application.'.format(cmdname=cmdname))
    parser.add_argument('path', help='The path part of the URL where the application is deployed.')
    return parser

# pylint: disable=too-many-public-methods
class InteractiveTomcatManager(cmd2.Cmd):
    """An interactive command line tool for the Tomcat Manager web application.

    each command sets the value of the instance variable exit_code, which follows
    bash behavior for exit codes (available via $?)
    """

    EXIT_CODES = {
        # 'number': 'name'
        0: 'success',
        1: 'error',
        2: 'usage',
        127: 'command_not_found',
    }

    # Possible boolean values
    BOOLEAN_VALUES = {'1': True, 'yes': True, 'y': True, 'true': True,
                      't': True, 'on': True,
                      '0': False, 'no': False, 'n': False,
                      'false': False, 'f': False, 'off': False}

    exit_codes = AttrDict()
    for _code, _title in EXIT_CODES.items():
        exit_codes[_title] = _code

    # for configuration
    app_name = 'tomcat-manager'
    app_author = 'tomcatmanager'
    config = None

    # new settings must to be defined at the class, not the instance
    timeout = 10
    status_prefix = '--'

    @property
    def status_to_stdout(self):
        """Proxy property for feedback_to_output."""
        return self.feedback_to_output

    @status_to_stdout.setter
    def status_to_stdout(self, value):
        """Proxy property for feedback_to_output."""
        self.feedback_to_output = value


    def __init__(self):
        # settings for cmd2.Cmd
        self.allow_cli_args = False

        self.abbrev = False
        self.echo = False
        unused = ['abbrev', 'continuation_prompt', 'feedback_to_output']
        for setting in unused:
            try:
                self.settable.pop(setting)
            except KeyError:
                pass
        self.settable.update({'echo': 'For piped input, echo command to output'})
        self.settable.update({'status_to_stdout': 'Status information to stdout instead of stderr'})
        self.settable.update({'editor': 'Program used to edit files'})
        self.settable.update({'timeout': 'Seconds to wait for HTTP connections'})
        self.settable.update({'status_prefix': 'String to prepend to all status output'})
        self.settable.update({'debug': 'Show stack trace for exceptions'})
        self.prompt = '{}> '.format(self.app_name)
        cmd2.Cmd.shortcuts.update({'$?': 'exit_code'})

        cmd2.Cmd.__init__(self)

        # initialize configuration
        self.appdirs = appdirs.AppDirs(self.app_name, self.app_author)
        self.load_config()

        # prepare our own stuff
        self.tomcat = tm.TomcatManager()
        self.tomcat.timeout = self.timeout
        self.exit_code = None

    ###
    #
    # Override cmd2.Cmd methods.
    #
    ###
    def poutput(self, msg, end='\n'):
        """
        Convenient shortcut for self.stdout.write();
        by default adds newline to end if not already present.

        Also handles BrokenPipeError exceptions for when a commands's output
        has been piped to another process and that process terminates before
        the cmd2 command is finished executing.

        :param msg: str - message to print to current stdout - anyting
                          convertible to a str with '{}'.format() is OK
        :param end: str - string appended after the end of the message if
                          not already present, default a newline
        """
        if msg is not None:
            try:
                msg_str = '{}'.format(msg)
                self.stdout.write(msg_str)
                if not msg_str.endswith(end):
                    self.stdout.write(end)
            except BrokenPipeError:
                # This occurs if a command's output is being piped to another
                # process and that process closes before the command is
                # finished.
                pass

    def perror(self, errmsg, exception_type=None, traceback_war=True):
        """
        Print an error message or an exception.

        :param: msg             The error message to print. If None, then
                                print information about the exception
                                currently beging handled.
        :param: exception_type  From superclass. Ignored here.
        :param: traceback_war   From superclass. Ignored here.

        If debug=True, you will get a full stack trace, otherwise just the
        exception.
        """
        if errmsg:
            sys.stderr.write('{}\n'.format(errmsg))
        else:
            _type, _exception, _traceback = sys.exc_info()
            if _exception:
                if self.debug:
                    output = ''.join(traceback.format_exception(_type, _exception, _traceback))
                else:
                    output = ''.join(traceback.format_exception_only(_type, _exception))
                sys.stderr.write(output)

    def pfeedback(self, msg):
        """
        Print nonessential feedback.

        Set quiet=True to supress all feedback. If feedback_to_output=True,
        then feedback will be included in the output stream. Otherwise, it
        will be sent to sys.stderr.
        """
        if not self.quiet:
            fmt = '{}{}\n'
            if self.feedback_to_output:
                self.poutput(fmt.format(self.status_prefix, msg))
            else:
                sys.stderr.write(fmt.format(self.status_prefix, msg))

    def emptyline(self):
        """Do nothing on an empty line"""
        pass

    def default(self, statement):
        """what to do if we don't recognize the command the user entered"""
        self.exit_code = self.exit_codes.command_not_found
        command = statement.parsed['command']
        self.perror('unknown command: {}'.format(command))

    ###
    #
    # Convenience and shared methods.
    #
    ###
    def docmd(self, func, *args, **kwargs):
        """Call a function and return, printing any exceptions that occur

        Sets exit_code to 0 and calls {func}. If func throws a TomcatError,
        set exit_code to 1 and print the exception
        """
        self.exit_code = self.exit_codes.success
        r = func(*args, **kwargs)
        try:
            r.raise_for_status()
        except tm.TomcatError as err:
            self.exit_code = self.exit_codes.error
            self.perror(str(err))
        return r

    def show_help_from(self, argparser):
        """Set exit code and output help from an argparser."""
        self.exit_code = self.exit_codes.success
        self.poutput(argparser.format_help())

    def _which_server(self):
        """
        What url are we connected to and who are we connected as.

        Returns None if '.url` is None.
        """
        out = None
        if self.tomcat.url:
            out = 'connected to {}'.format(self.tomcat.url)
            if self.tomcat.user:
                out += ' as {}'.format(self.tomcat.user)
        return out

    ###
    #
    # user accessable commands for configuration and settings
    #
    ###
    def do_config(self, args):
        """Show the location of the user configuration file."""
        if len(args.split()) == 1:
            action = args.split()[0]
            if action == 'file':
                self.poutput(self.config_file)
                self.exit_code = self.exit_codes.success
            elif action == 'edit':
                self.config_edit()
            else:
                self.help_config()
                self.exit_code = self.exit_codes.error
        else:
            self.help_config()
            self.exit_code = self.exit_codes.error

    def config_edit(self):
        """Edit the user configuration file."""
        if not self.editor:
            self.perror("no editor: use 'set editor={path}' to specify one")
            self.exit_code = self.exit_codes.error
            return

        # ensure the configuration directory exists
        configdir = os.path.dirname(self.config_file)
        if not os.path.exists(configdir):
            os.makedirs(configdir)

        # go edit the file
        cmd = '"{}" "{}"'.format(self.editor, self.config_file)
        self.pfeedback("executing {}".format(cmd))
        os.system(cmd)

        # read it back in and apply it
        self.pfeedback("reloading configuration")
        self.load_config()
        self.exit_code = self.exit_codes.success

    def help_config(self):
        """Show help for the 'config' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: config {action}

Manage the user configuration file.

action is one of the following:

  file  show the location of the user configuration file
  edit  edit the user configuration file""")

    def do_show(self, arg, opts=None):
        """
        Show all settings or a specific setting.

        Overrides cmd2.Cmd.do_show()
        """
        if len(arg.split()) > 1:
            self.help_show()
            self.exit_code = self.exit_codes.error
            return

        param = arg.strip().lower()
        result = {}
        maxlen = 0
        for setting in self.settable:
            if (not param) or (setting == param):
                val = str(getattr(self, setting))
                result[setting] = '{}={}'.format(setting, self._pythonize(val))
                maxlen = max(maxlen, len(result[setting]))
        # make a little extra space
        maxlen += 1
        if result:
            for setting in sorted(result):
                self.poutput('{} # {}'.format(result[setting].ljust(maxlen), self.settable[setting]))
            self.exit_code = self.exit_codes.success
        else:
            self.perror("unknown setting: '{}'".format(param))
            self.exit_code = self.exit_codes.error

    def help_show(self):
        """Show help for the 'show' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: show [setting]

Show one or more settings and their values.

[setting]  Optional name of the setting to show the value for. If omitted
           show the values of all settings.""")

    def do_settings(self, args):
        """Synonym for 'show' command."""
        self.do_show(args)

    def help_settings(self):
        """Show help for the 'settings' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: settings [setting]

Show one or more settings and their values.

[setting]  Optional name of the setting to show the value for. If omitted
           show the values of all settings.""")

    def do_set(self, arg):
        """
        Change a setting.

        Overrides cmd2.Cmd.do_set()
        """
        if arg:
            config = EvaluatingConfigParser()
            setting_string = "[settings]\n{}".format(arg)
            try:
                config.read_string(setting_string)
            except configparser.ParsingError:
                self.perror('invalid syntax: try {setting}={value}')
                self.exit_code = self.exit_codes.error
                return
            for param_name in config['settings']:
                if param_name in self.settable:
                    try:
                        self._change_setting(param_name, config['settings'][param_name])
                        self.exit_code = self.exit_codes.success
                    except ValueError as err:
                        if self.debug:
                            self.perror(None)
                        else:
                            self.perror(err)
                        self.exit_code = self.exit_codes.error
                else:
                    self.perror("unknown setting: '{}'".format(param_name))
                    self.exit_code = self.exit_codes.error
        else:
            self.do_show(arg, None)

    def help_set(self):
        """Show help for the 'set' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: set {setting}={value}

Change a setting.

  setting  Any one of the valid settings. Use 'show' to see a list of valid
           settings.
  value    The value for the setting.
""")

    ###
    #
    # other methods and properties related to configuration and settings
    #
    ###
    @property
    def config_file(self):
        """
        The location of the user configuration file.

        :return: The full path to the user configuration file, or None
                 if self.app_name has not been defined.
        """
        filename = self.app_name + '.ini'
        return os.path.join(self.appdirs.user_config_dir, filename)

    def load_config(self):
        """Open and parse the user config file and set self.config."""
        config = None
        if self.config_file is not None:
            config = EvaluatingConfigParser()
            try:
                with open(self.config_file, 'r') as fobj:
                    config.read_file(fobj)
            except FileNotFoundError:
                pass
        try:
            settings = config['settings']
            for key in settings:
                self._change_setting(key, settings[key])
        except KeyError:
            pass
        except ValueError:
            pass
        self.config = config

    def _change_setting(self, param_name, val):
        """
        Apply a change to a setting, calling a hook if it is defined.

        This method is intended to only be called when the user requests the setting
        to be changed, either interactively or by loading the configuration file.

        param_name must be in settable or this method with throw a ValueError
        some parameters only accept boolean values, if you pass something that can't
        be converted to a boolean, throw a ValueError

        Call _onchange_{param_name}(old, new) after the setting changes value.
        """
        if param_name in self.settable:
            current_val = getattr(self, param_name)
            type_ = type(current_val)
            if type_ == bool:
                val = self.convert_to_boolean(val)
            elif type_ == int:
                try:
                    val = int(val)
                except ValueError:
                    # make a nicer error message
                    raise ValueError("invalid integer: '{}'".format(val))
            setattr(self, param_name, val)
            if current_val != val:
                try:
                    onchange_hook = getattr(self, '_onchange_{}'.format(param_name))
                    onchange_hook(old=current_val, new=val)
                except AttributeError:
                    pass
        else:
            raise ValueError

    def _onchange_timeout(self, old, new):
        """Pass the new timeout through to the TomcatManager object."""
        self.tomcat.timeout = new

    def convert_to_boolean(self, value):
        """Return a boolean value translating from other types if necessary."""
        if isinstance(value, bool) is True:
            return value
        else:
            if str(value).lower() not in self.BOOLEAN_VALUES:
                if value is None or value == '':
                    raise ValueError('invalid syntax: must be true-ish or false-ish')
                else:
                    raise ValueError("invalid syntax: not a boolean: '{}'".format(value))
            return self.BOOLEAN_VALUES[value.lower()]

    @staticmethod
    def _pythonize(value):
        """Transform value into something the python interpreter can parse.

        Transform value into pvalue such that:

            value = ast.literal_eval(pvalue)

        This isn't quite true, because if there are no spaces or quote marks in value, then

            pvalue = value
        """
        single_quote = "'"
        double_quote = '"'
        pvalue = value
        if (single_quote in value) and (double_quote in value):
            # use sq as the outer quote, which means we have to
            # backslash all the other sq in the string
            value = value.replace(single_quote, '\\' + single_quote)
            pvalue = "'{}'".format(value)
        elif single_quote in value:
            pvalue = '"{}"'.format(value)
        elif double_quote in value:
            pvalue = "'{}'".format(value)
        elif ' ' in value:
            pvalue = "'{}'".format(value)
        return pvalue

    ###
    #
    # Connecting to Tomcat
    #
    ###
    def do_connect(self, args):
        """Connect to a tomcat manager instance."""
        url = None
        user = None
        password = None
        args_list = args.split()

        if len(args_list) == 1:
            # check the configuration file for values
            server = args_list[0]
            if self.config.has_section(server):
                if self.config.has_option(server, 'url'):
                    url = self.config[server]['url']
                if self.config.has_option(server, 'user'):
                    user = self.config[server]['user']
                if self.config.has_option(server, 'password'):
                    password = self.config[server]['password']
            else:
                url = args_list[0]
        elif len(args_list) == 2:
            url = args_list[0]
            user = args_list[1]
        elif len(args_list) == 3:
            url = args_list[0]
            user = args_list[1]
            password = args_list[2]
        else:
            self.help_connect()
            self.exit_code = self.exit_codes.usage
            return

        # prompt for password if necessary
        if url and user and not password:
            password = getpass.getpass()

        try:
            r = self.tomcat.connect(url, user, password)
            if r.ok:
                self.pfeedback(self._which_server())
                self.exit_code = self.exit_codes.success
            else:
                if self.debug:
                    # raise the exception and print the output
                    try:
                        r.raise_for_status()
                    except Exception:
                        self.perror(None)
                        self.exit_code = self.exit_codes.error
                else:
                    # need to see whether we got an http error or whether
                    # tomcat wasn't at the url
                    if r.response.status_code == requests.codes.ok:
                        # there was some problem with the request, but we
                        # got http 200 OK. That means there was no tomcat
                        # at the url
                        self.perror('tomcat manager not found at {}'.format(url))
                    elif r.response.status_code == requests.codes.not_found:
                        # we connected, but the url was bad. No tomcat there
                        self.perror('tomcat manager not found at {}'.format(url))
                    else:
                        self.perror('http error: {} {}'.format(r.response.status_code, responses[r.response.status_code]))
                    self.exit_code = self.exit_codes.error
        except requests.exceptions.ConnectionError:
            if self.debug:
                self.perror(None)
            else:
                self.perror('connection error')
            self.exit_code = self.exit_codes.error
        except requests.exceptions.Timeout:
            if self.debug:
                self.perror(None)
            else:
                self.perror('connection timeout')
            self.exit_code = self.exit_codes.error

    def help_connect(self):
        """Show help for the 'connect' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: connect url [user] [password]
       connect config_name

Connect to a tomcat manager instance.

config_name  A section from the config file. This must contain url, user,
             and password values.

url          The url where the Tomcat Manager web app is located.
user         Optional user to use for authentication.
password     Optional password to use for authentication.

If you specify a user and no password, you will be prompted for the
password. If you don't specify a userid or password, attempt to connect
with no authentication.""")

    @requires_connection
    def do_which(self, args):
        """Show the url of the tomcat server you are connected to."""
        if args:
            self.help_which()
            self.exit_code = self.exit_codes.usage
        else:
            self.poutput(self._which_server())

    def help_which(self):
        """Show help for the 'which' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: which

Show the url of the tomcat server you are connected to.""")

    ###
    #
    # commands for managing applications
    #
    ###
    def deploy_local(self, args, update=False):
        warfile = os.path.expanduser(args.warfile)
        with open(warfile, 'rb') as fileobj:
            self.exit_code = self.exit_codes.success
            self.docmd(self.tomcat.deploy_localwar, args.path, fileobj,
                       version=args.version, update=update)

    def deploy_server(self, args, update=False):
        self.exit_code = self.exit_codes.success
        self.docmd(self.tomcat.deploy_serverwar, args.path, args.warfile,
                   version=args.version, update=update)

    def deploy_context(self, args, update=False):
        self.exit_code = self.exit_codes.success
        self.docmd(self.tomcat.deploy_servercontext, args.path, args.contextfile,
                   warfile=args.warfile, version=args.version, update=update)

    deploy_parser = argparse.ArgumentParser(prog='deploy', description='Install a war file containing a tomcat application in the tomcat server')
    deploy_subparsers =  deploy_parser.add_subparsers(title='methods', dest='method')
    # local subparser
    deploy_local_parser = deploy_subparsers.add_parser('local', description='transmit a locally available warfile to the server', help='transmit a locally available warfile to the server')
    deploy_local_parser.add_argument('-v', '--version', help='version string to associate with this deployment')
    deploy_local_parser.add_argument('warfile')
    deploy_local_parser.add_argument('path')
    deploy_local_parser.set_defaults(func=deploy_local)
    # server subparser
    deploy_server_parser = deploy_subparsers.add_parser('server', description='deploy a warfile already on the server', help='deploy a warfile already on the server')
    deploy_server_parser.add_argument('-v', '--version', help='version string to associate with this deployment')
    deploy_server_parser.add_argument('warfile')
    deploy_server_parser.add_argument('path')
    deploy_server_parser.set_defaults(func=deploy_server)
    # context subparser
    deploy_context_parser = deploy_subparsers.add_parser('context', description='deploy a contextfile already on the server', help='deploy a contextfile already on the server')
    deploy_context_parser.add_argument('-v', '--version', help='version string to associate with this deployment')
    deploy_context_parser.add_argument('contextfile')
    deploy_context_parser.add_argument('warfile', nargs='?')
    deploy_context_parser.add_argument('path')
    deploy_context_parser.set_defaults(func=deploy_context)

    deploy_subcommands = ['local', 'server', 'context']
    @requires_connection
    @cmd2.with_argparser(deploy_parser, deploy_subcommands)
    def do_deploy(self, args):
        try:
            args.func(self, args, update=False)
        except AttributeError:
            self.do_help('deploy')

    def help_deploy(self):
        self.show_help_from(self.deploy_parser)

    @requires_connection
    @cmd2.with_argparser(deploy_parser, deploy_subcommands)
    def do_redeploy(self, args):
        try:
            args.func(self, args, update=True)
        except AttributeError:
            self.do_help('redeploy')

    def help_redeploy(self):
        self.show_help_from(self.deploy_parser)

    

    def _parse_args(self, parser, cmdline):
        """Use argparse to parse list command arguments"""
        # set exit codes so if we raise exceptions, we have the right value
        self.exit_code = self.exit_codes.error
        lexed_cmdline = cmd2.parse_quoted_string(cmdline)
        # no parse error, assume we get a usage error
        self.exit_code = self.exit_codes.usage
        args = parser.parse_args(lexed_cmdline)
        # no usage error, assume success
        self.exit_code = self.exit_codes.success
        return args        


    undeploy_parser = _path_version_parser('undeploy', 'Remove an application at a given path from the tomcat server.')

    @requires_connection
    def do_undeploy(self, cmdline):
        """Remove an application at a given path from the tomcat server."""
        args = self._parse_args(self.undeploy_parser, cmdline)
        self.docmd(self.tomcat.undeploy, args.path, args.version)

    def help_undeploy(self):
        """Help for the 'undeploy' command."""
        self.show_help_from(self.undeploy_parser)


    start_parser = _path_version_parser('start', "Start a tomcat application that has been deployed but isn't running.")

    @requires_connection
    def do_start(self, cmdline):
        """Start a tomcat application that has been deployed but isn't running."""
        args = self._parse_args(self.start_parser, cmdline)
        self.docmd(self.tomcat.start, args.path, args.version)

    def help_start(self):
        """Help for the 'start' command."""
        self.show_help_from(self.start_parser)



    stop_parser = _path_version_parser('stop', "Stop a running tomcat application and leave it deployed on the server.")

    @requires_connection
    def do_stop(self, cmdline):
        """Stop a tomcat application and leave it deployed on the server."""
        args = self._parse_args(self.stop_parser, cmdline)
        self.docmd(self.tomcat.stop, args.path, args.version)

    def help_stop(self):
        """Help for the 'stop' command."""
        self.show_help_from(self.stop_parser)


    reload_parser = _path_version_parser('reload', 'Start and stop a tomcat application.')

    @requires_connection
    def do_reload(self, cmdline):
        """Start and stop a tomcat application."""
        args = self._parse_args(self.reload_parser, cmdline)
        self.docmd(self.tomcat.reload, args.path, args.version)

    def help_reload(self):
        """Help for the 'reload' application."""
        self.show_help_from(self.reload_parser)


    restart_parser = _path_version_parser('restart', 'Start and stop a tomcat application. Synonym for reload.')
    
    @requires_connection
    def do_restart(self, cmdline):
        """Start and stop a tomcat application. Synonym for reload."""
        self.do_reload(cmdline)

    def help_restart(self):
        """Show help for the 'restart' command."""
        self.show_help_from(self.restart_parser)


    sessions_parser = argparse.ArgumentParser(prog='sessions',
                      description='Show active sessions for a tomcat application.')
    sessions_parser.add_argument('-v', '--version', help='Optional version string of the application from which to show sessions. If the application was deployed with a version string, it must be specified in order to show sessions.')
    sessions_parser.add_argument('path', help='The path part of the URL where the application is deployed.')

    @requires_connection
    def do_sessions(self, cmdline):
        """Show active sessions for a tomcat application."""
        args = self._parse_args(self.sessions_parser, cmdline)
        r = self.docmd(self.tomcat.sessions, args.path, args.version)
        if r.ok:
            self.poutput(r.sessions)

    def help_sessions(self):
        """Help for the 'sessions' command."""
        self.show_help_from(self.sessions_parser)


    expire_parser = argparse.ArgumentParser(prog='sessions',
                      description='Show active sessions for a tomcat application.')
    expire_parser.add_argument('-v', '--version', help='Optional version string of the application from which to expire sessions. If the application was deployed with a version string, it must be specified in order to expire sessions.')
    expire_parser.add_argument('path', help='The path part of the URL where the application is deployed.')
    expire_parser.add_argument('idle', help='Expire sessions idle for more than this number of minutes. Use 0 to expire all sessions.')

    @requires_connection
    def do_expire(self, cmdline):
        """Expire idle sessions."""
        args = self.parse_args(self.expire_parser, cmdline)
        r = self.docmd(self.tomcat.expire, args.path, args.version, args.idle)
        if r.ok:
            self.poutput(r.sessions)

    def help_expire(self):
        """Help for the 'expire' command."""
        self.show_help_from(self.expire_parser)


    list_parser = argparse.ArgumentParser(
        prog='list',
        description='Show all installed applications',
    )
    raw_help = 'show apps without formatting'
    list_parser.add_argument('-r', '--raw', action='store_true', help=raw_help)
    state_help = 'only show apps in a given state'
    list_parser.add_argument('-s', '--state', choices=['running', 'stopped'], help=state_help)
    by_help = 'sort by state (default), or sort by path'
    list_parser.add_argument('-b', '--by', choices=['state', 'path'], default='state', help=by_help)

    @requires_connection
    def do_list(self, cmdline):
        """Show all installed applications."""
        args = self._parse_args(self.list_parser, cmdline)
        response = self.docmd(self.tomcat.list)
        if not response.ok:
            return

        apps = self._list_process_apps(response.apps, args)

        self.exit_code = self.exit_codes.success
        if args.raw:
            for app in apps:
                self.poutput(app)
        else:
            fmt = '{:24.24} {:7.7} {:>8.8} {:36.36}'
            dashes = '-'*80
            self.poutput(fmt.format('Path', 'State', 'Sessions', 'Directory'))
            self.poutput(fmt.format(dashes, dashes, dashes, dashes))
            for app in apps:
                self.poutput(fmt.format(app.path, app.state, str(app.sessions), app.directory_and_version))

    def help_list(self):
        """Show help for the 'list' command."""
        self.show_help_from(self.list_parser)

    def _list_process_apps(self, apps, args):
        """
        Select and sort a list of `TomcatApplication` objects based on arguments

        We rely on the `TomcatApplication` object to determine the sort order.

        :return: a list of `TomcatApplication` objects
        """
        rtn = []
        # select the apps that should be included
        if args.state:
            rtn = filter(lambda app: app.state == args.state, apps)
        else:
            rtn = apps
        # now sort them
        if args.by == 'path':
            rtn = sorted(rtn, key=tm.models.TomcatApplication.sort_by_path_by_version_by_state)
        else:
            rtn = sorted(rtn, key=tm.models.TomcatApplication.sort_by_state_by_path_by_version)
        return rtn

    ###
    #
    # These commands that don't affect change, they just return some
    # information from the server.
    #
    ###
    @requires_connection
    def do_serverinfo(self, args):
        """Show information about the Tomcat server."""
        if args:
            self.help_serverinfo()
            self.exit_code = self.exit_codes.usage
        else:
            r = self.docmd(self.tomcat.server_info)
            self.poutput(r.result)

    def help_serverinfo(self):
        """Show help for the 'serverinfo' class."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: serverinfo

Show information about the Tomcat server.""")

    @requires_connection
    def do_status(self, args):
        """Show server status information in xml format."""
        if args:
            self.help_status()
            self.exit_code = self.exit_codes.usage
        else:
            r = self.docmd(self.tomcat.status_xml)
            root = xml.dom.minidom.parseString(r.status_xml)
            self.poutput(root.toprettyxml(indent='   '))

    def help_status(self):
        """Show help for the 'status' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: status

Show server status information in xml format.""")

    @requires_connection
    def do_vminfo(self, args):
        """Show diagnostic information about the jvm."""
        if args:
            self.help_vminfo()
            self.exit_code = self.exit_codes.usage
        else:
            response = self.docmd(self.tomcat.vm_info)
            self.poutput(response.vm_info)

    def help_vminfo(self):
        """Show help for the 'vminfo' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: vminfo

Show diagnostic information about the jvm.""")

    @requires_connection
    def do_sslconnectorciphers(self, args):
        """Show SSL/TLS ciphers configured for each connector."""
        if args:
            self.help_sslconnectorciphers()
            self.exit_code = self.exit_codes.usage
        else:
            response = self.docmd(self.tomcat.ssl_connector_ciphers)
            self.poutput(response.ssl_connector_ciphers)

    def help_sslconnectorciphers(self):
        """Show help for the 'sslconnectorciphers' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: sslconnectorciphers

Show SSL/TLS ciphers configured for each connector.""")

    @requires_connection
    def do_threaddump(self, args):
        """Show a jvm thread dump."""
        if args:
            self.help_threaddump()
            self.exit_code = self.exit_codes.usage
        else:
            response = self.docmd(self.tomcat.thread_dump)
            self.poutput(response.thread_dump)

    def help_threaddump(self):
        """Show help for the 'threaddump' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: threaddump

Show a jvm thread dump.""")

    @requires_connection
    def do_resources(self, args):
        """Show global JNDI resources configured in Tomcat."""
        if len(args.split()) in [0, 1]:
            try:
                # this nifty line barfs if there are other than 1 argument
                type_, = args.split()
            except ValueError:
                type_ = None
            r = self.docmd(self.tomcat.resources, type_)
            for resource, classname in iter(sorted(r.resources.items())):
                self.poutput('{}: {}'.format(resource, classname))
        else:
            self.help_resources()
            self.exit_code = self.exit_codes.usage

    def help_resources(self):
        """Show help for the 'resources' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: resources [class_name]

Show global JNDI resources configured in Tomcat.

class_name  Optional fully qualified java class name of the resource type
            to show.""")

    @requires_connection
    def do_findleakers(self, args):
        """Show tomcat applications that leak memory."""
        if args:
            self.help_findleakers()
            self.exit_code = self.exit_codes.usage
        else:
            response = self.docmd(self.tomcat.find_leakers)
            for leaker in response.leakers:
                self.poutput(leaker)

    def help_findleakers(self):
        """Show help for the 'findleakers' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: findleakers

Show tomcat applications that leak memory.

WARNING: this triggers a full garbage collection on the server. Use with
extreme caution on production systems.""")

    ###
    #
    # miscellaneous user accessible commands
    #
    ###
    def do_exit(self, args):
        """Exit the interactive command prompt."""
        self.exit_code = self.exit_codes.success
        return self._STOP_AND_EXIT

    def do_quit(self, arg):
        """Synonym for the 'exit' command."""
        return self.do_exit(arg)

    def do_eof(self, arg):
        """Exit on the end-of-file character."""
        return self.do_exit(arg)

    def do_version(self, args):
        """Show version information."""
        self.exit_code = self.exit_codes.success
        self.poutput(tm.VERSION_STRING)

    def help_version(self):
        """Show help for the 'version' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: version

Show version information.""")

    def do_exit_code(self, args):
        """Show the value of the exit_code variable."""
        # don't set the exit code here, just show it
        self.poutput(self.exit_code)

    def help_exit_code(self):
        """Show help for the 'exit_code' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: exit_code

Show the value of the exit_code variable, similar to $? in ksh/bash.""")

    def do_license(self, args):
        """Show license information."""
        self.exit_code = self.exit_codes.success
        self.poutput("""
Copyright 2007 Jared Crapo

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
""")

    def help_license(self):
        """Show help for the 'license' command."""
        self.exit_code = self.exit_codes.success
        self.poutput("""Usage: license

Show license information.""")


# pylint: disable=too-many-ancestors
class EvaluatingConfigParser(configparser.ConfigParser):
    """Subclass of configparser.ConfigParser which evaluates values on get()."""
    # pylint: disable=arguments-differ
    def get(self, section, option, **kwargs):
        val = super().get(section, option, **kwargs)
        if "'" in val or '"' in val:
            try:
                val = ast.literal_eval(val)
            except ValueError:
                pass
        return val
