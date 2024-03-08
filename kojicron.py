#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from argparse import ArgumentParser, Namespace
from configparser import ConfigParser
import fnmatch
import logging
import logging.handlers
import subprocess
import sys


ERR_CONFIG = 3
ERR_CANT_GET_TAG_LIST = 4
ERR_NO_TAGS_TO_REGEN = 5
ERR_CANT_AUTH_TO_KOJI = 6
ERR_CANT_REGEN_REPO = 7

MB = 1 << 20

_debug = False
_log = logging.getLogger(__name__)


class ProgramError(RuntimeError):
    """Class for fatal errors during execution.  The `returncode` parameter
    should be used as the exit code for the program.
    """

    def __init__(self, returncode, *args):
        super().__init__(*args)
        self.returncode = returncode


class ConfigError(ProgramError):
    """Class for errors with the configuration"""

    def __init__(self, *args):
        super().__init__(ERR_CONFIG, *args)

    def __str__(self):
        return f"Config error: {super().__str__()}"


def run(*args, **kwargs):
    """Wrapper around subprocess.run() that logs the command being run if
    debug output is requested.
    """
    if _debug:
        _log.debug("running %r %r", args, kwargs)
    return subprocess.run(*args, **kwargs)


class RunKoji:
    """A helper class for running arbitrary Koji commands"""

    def __init__(self, config_path, config_section):
        self.config_path = config_path
        self.config_section = config_section

    def __call__(self, *args, **kwargs):
        koji_cmd_base = [
            "koji",
            "-q",
            "--config=" + self.config_path,
            "--profile=" + self.config_section,
        ]
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        kwargs.setdefault("encoding", "latin-1")

        return run(
            koji_cmd_base + list(args),
            **kwargs,
        )


def validate_config(config: ConfigParser):
    try:
        # fmt: off
        assert "kojicron" in config,                        "[kojicron] section missing"
        kcconfig = config["kojicron"]
        for required_option in ("server", "authtype", "included_tags"):
            assert kcconfig.get(required_option),          f"{required_option} not provided"
        assert kcconfig["server"].startswith("https://"),   "server is not an HTTPS URL"
        assert kcconfig["server"].endswith("/kojihub"),     "server is not a koji-hub XMLRPC endpoint (/kojihub)"
        if kcconfig["authtype"] == "ssl":
            assert kcconfig.get("cert"),                    "cert not provided for ssl authtype"
        else:
            assert kcconfig["authtype"] == "gssapi",        "authtype is not 'ssl' or 'gssapi'"
            assert kcconfig.get("principal"),               "principal not provided for gssapi authtype"
        # fmt: on
    except AssertionError as err:
        raise ConfigError(str(err))


def regen_a_tag(tag: str, run_koji: RunKoji, wait: bool) -> bool:
    """Runs regen-repo on a single tag. Returns success or failure.

    Waits for the regen-repo to complete if `wait` is True.
    """
    global _log

    if wait:
        _log.info("Launching regen-repo for tag %s", tag)
        ret = run_koji("regen-repo", tag)
    else:
        _log.info("Queueing regen-repo for tag %s", tag)
        ret = run_koji("regen-repo", "--nowait", tag)
    if ret.returncode != 0:
        _log.error(
            "Return code %d doing regen-repo %s.\nStdout:\n%s\nStderr:\n%s",
            ret.returncode,
            tag,
            ret.stdout,
            ret.stderr,
        )
        return False
    else:
        _log.debug(
            "regen-repo %s succeeded.\nStdout:\n%s\nStderr:\n%s",
            tag,
            ret.stdout,
            ret.stderr,
        )
        return True


def get_boolean_option(name: str, args: Namespace, config: ConfigParser) -> bool:
    """Gets the value of a boolean config file option, which can also be turned on by
    a command-line argument of the same name.
    Raise ConfigError if the option is not a boolean.
    """
    try:
        cmdarg = getattr(args, name, None)
        if cmdarg is None:
            return config["kojicron"].getboolean(name, fallback=False)
        else:
            return bool(cmdarg)
    except ValueError:
        raise ConfigError(f"'{name}' must be a boolean")


def setup_logging(args: Namespace, config: ConfigParser):
    """Sets up logging, given the config and the command-line arguments.

    Logs are written to a logfile if one is defined. In addition, log to stderr
    if it's a tty.
    """
    loglevel = logging.DEBUG if _debug else logging.INFO
    _log.setLevel(loglevel)
    if sys.stderr.isatty():
        ch = logging.StreamHandler()
        ch.setLevel(loglevel)
        chformatter = logging.Formatter("%(message)s")
        ch.setFormatter(chformatter)
        _log.addHandler(ch)
    if args.logfile:
        logfile = args.logfile
    else:
        logfile = config.get("kojicron", "logfile", fallback="")
    if logfile:
        rfh = logging.handlers.RotatingFileHandler(
            logfile,
            maxBytes=500 * MB,
            backupCount=1,
        )
        rfh.setLevel(loglevel)
        rfhformatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s",
        )
        rfh.setFormatter(rfhformatter)
        _log.addHandler(rfh)


def parse_command_line(argv):
    """Handle command-line arguments"""
    parser = ArgumentParser(prog=argv[0])
    parser.add_argument(
        "--config",
        default="/etc/kojicron/kojicron.conf",
        help="Location of config file (default: %(default)s)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Output debug messages",
    )
    parser.add_argument(
        "--logfile",
        default="",
        help="Logfile to write output to (no default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't run, just print the repos that would be regenerated",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for each regen to complete before starting the next (default: false)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_false",
        dest="wait",
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="On regen failure, keep going with remaining tags instead of exiting (default: false)",
    )
    parser.add_argument(
        "--no-continue-on-failure",
        action="store_false",
        dest="continue_on_failure",
    )
    parser.set_defaults(wait=None, continue_on_failure=None)

    return parser.parse_argv(argv[1:])


def main(argv=None):
    global _debug

    args = parse_command_line(argv or sys.argv)

    config = ConfigParser()
    config.read(args.config)
    validate_config(config)

    _debug = get_boolean_option("debug", args, config)
    wait = get_boolean_option("wait", args, config)
    continue_on_failure = get_boolean_option("continue_on_failure", args, config)

    setup_logging(args, config)
    run_koji = RunKoji(args.config, config_section="kojicron")

    _log.info("kojicron starting")

    ret = run_koji("--noauth", "list-tags")
    if ret.returncode != 0:
        raise ProgramError(
            ERR_CANT_GET_TAG_LIST,
            f"Return code {ret.returncode} getting tag list from server.\n"
            f"Stdout:\n{ret.stdout}\n"
            f"Stderr:\n{ret.stderr}",
        )

    tags = ret.stdout.splitlines()
    tags_to_regen = set()
    patterns = config["kojicron"]["included_tags"].split()
    for pattern in patterns:
        tags_to_regen.update(fnmatch.filter(tags, pattern))

    if not tags_to_regen:
        raise ProgramError(
            ERR_NO_TAGS_TO_REGEN,
            f"No tags in Koji match the given patterns. Patterns are: {patterns}",
        )

    if args.dry_run:
        print("Would regen the following tags:\n" + "\n".join(sorted(tags_to_regen)))
        return 0

    ret = run_koji("hello")
    if ret.returncode != 0:
        raise ProgramError(
            ERR_CANT_AUTH_TO_KOJI,
            f"Return code {ret.returncode} authenticating to Koji.\n"
            f"Stdout:\n{ret.stdout}\n"
            f"Stderr:\n{ret.stderr}",
        )

    failed_tags = set()
    # doing a while loop instead of iteration to keep track of the remaining tags
    while tags_to_regen:
        tag = tags_to_regen.pop()
        ok = regen_a_tag(tag, run_koji, wait)
        if not ok:
            if not continue_on_failure:
                raise ProgramError(
                    ERR_CANT_REGEN_REPO,
                    f"Error doing regen-repo {tag}.  Remaining tags: {tags_to_regen}",
                )
            _log.info("Continuing")
            failed_tags.add(tag)
    # end while

    if failed_tags:
        raise ProgramError(
            ERR_CANT_REGEN_REPO,
            f"The following tag(s) failed to regen: {sorted(failed_tags)}",
        )

    _log.info("kojicron successful")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProgramError as e:
        _log.error("%s", e, exc_info=_debug)
        sys.exit(e.returncode)
