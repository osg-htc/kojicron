#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from argparse import ArgumentParser, Namespace
from configparser import ConfigParser
import fnmatch
import logging
import logging.handlers
import subprocess
import sys
from typing import Collection, List, Optional, Set


ERR_CONFIG = 3
ERR_CANT_GET_TAG_LIST = 4
ERR_NO_TAGS_TO_REGEN = 5
ERR_CANT_AUTH_TO_KOJI = 6
ERR_CANT_REGEN_REPO = 7

CFG_SECTION = "kojicron"

MB = 1 << 20
LOG_MAX_SIZE = 500 * MB

_debug = False
_log = logging.getLogger(__name__)


class ProgramError(RuntimeError):
    """
    Class for fatal errors during execution.  The `returncode` parameter
    should be used as the exit code for the program.
    """

    def __init__(self, returncode, *args):
        super().__init__(*args)
        self.returncode = returncode


class KojiError(ProgramError):
    """
    Class for errors while talking to Koji.  The `returncode` parameter
    should be used as the exit code for the program.
    """

    def __str__(self):
        return f"Koji error: {super().__str__()}"


class ConfigError(ProgramError):
    """Class for errors with the configuration"""

    def __init__(self, *args):
        super().__init__(ERR_CONFIG, *args)

    def __str__(self):
        return f"Config error: {super().__str__()}"


class KojiCron:
    """
    A class for interacting with Koji via the cli.

    The koji cli gets params for which server to contact, how to auth
    to the server, etc., from a config file; also, we can tell it what
    section of the config file to take options from.

    Args:
        config_path: Path to the config file koji commands will use
        config_section: The section of the config file koji commands
            will use
    """

    def __init__(self, config_path: str, config_section: str):
        self.config_path = config_path
        self.config_section = config_section
        self._koji_cmd_base = [
            "koji",
            "-q",
            "--config=" + self.config_path,
            "--profile=" + self.config_section,
        ]

    def koji(self, *args, **kwargs):
        """
        A wrapper around `subprocess.run` that runs `koji` with
        the arguments for the right config file and config section.

        Args:
            *args: same as subprocess.run()
            **kwargs: same as subprocess.run()

        Returns:
            same as subprocess.run()
        """
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        kwargs.setdefault("encoding", "latin-1")

        cmd = self._koji_cmd_base + list(args)

        if _debug:
            _log.debug("running %r %r", cmd, kwargs)

        return subprocess.run(
            cmd,
            **kwargs,
        )

    def get_tag_list(self) -> List[str]:
        """
        Return a list of tags available in Koji
        Raises: KojiError if we can't talk to Koji.
        """
        ret = self.koji("--noauth", "list-tags")
        if ret.returncode != 0:
            raise KojiError(
                ERR_CANT_GET_TAG_LIST,
                f"Return code {ret.returncode} getting tag list from server.\n"
                f"Stdout:\n{ret.stdout}\n"
                f"Stderr:\n{ret.stderr}",
            )
        tags = ret.stdout.splitlines()
        return tags

    def get_tags_to_regen(self, included_tags: Collection[str]) -> Set[str]:
        """
        Query Koji for the available tags, then filter them by the
        glob patterns provided in included_tags.

        Args:
            included_tags: One or more globs to narrow down the
                tags list by.  Only tags that match at least one of the
                globs will be regened.

        Returns:
            A set of tags to regen.
        """
        if isinstance(included_tags, str):
            included_tags = [included_tags]
        tags = self.get_tag_list()
        tags_to_regen = set()
        for pattern in included_tags:
            tags_to_regen.update(fnmatch.filter(tags, pattern))
        return tags_to_regen

    def verify_auth(self) -> None:
        """Test if authentication to Koji works; raise KojiError if not."""
        ret = self.koji("hello")
        if ret.returncode != 0:
            raise KojiError(
                ERR_CANT_AUTH_TO_KOJI,
                f"Return code {ret.returncode} authenticating to Koji.\n"
                f"Stdout:\n{ret.stdout}\n"
                f"Stderr:\n{ret.stderr}",
            )

    def regen_a_tag(self, tag: str, wait: bool) -> bool:
        """
        Runs regen-repo on a single tag.

        Args:
            tag: the tag to regen
            wait: whether to wait for regen-repo to complete

        Returns:
            True if calling regen-repo was successful.  If `wait` is
            True, this also means the regen itself was successful;
            otherwise it just means that the task was submitted.
        """
        global _log

        if wait:
            _log.info("Launching regen-repo for tag %s", tag)
            ret = self.koji("regen-repo", tag)
        else:
            _log.info("Queueing regen-repo for tag %s", tag)
            ret = self.koji("regen-repo", "--nowait", tag)
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

    def regen_tags(
        self,
        tags_to_regen: Collection[str],
        continue_on_failure: bool,
        wait: bool,
    ) -> Set[str]:
        """
        Regen one or more tags.

        Args:
            tags_to_regen: the tags to regen; tags must exist in Koji
            continue_on_failure: on failure, continue with the
                remaining tags instead of bailing out immediately
            wait: wait for each tag to be regenerated before starting
                the next

        Returns:
            The set of tags we couldn't regenerate

        Raises:
            KojiError: if we couldn't regen a tag and
                continue_on_failure is False
        """
        if isinstance(tags_to_regen, str):
            tags_to_regen = [tags_to_regen]
        failed_tags = set()
        remaining_tags_to_regen = set(tags_to_regen)
        # doing a while loop instead of iteration to keep track of the remaining tags
        while remaining_tags_to_regen:
            tag = remaining_tags_to_regen.pop()
            ok = self.regen_a_tag(tag, wait)
            if not ok:
                if not continue_on_failure:
                    raise KojiError(
                        ERR_CANT_REGEN_REPO,
                        f"Error doing regen-repo {tag}.  Remaining tags: {remaining_tags_to_regen}",
                    )
                _log.info("Continuing")
                failed_tags.add(tag)
        # end while
        return failed_tags


def validate_config(config: ConfigParser) -> None:
    """Validate the config file; raise ConfigError if validation fails."""

    if CFG_SECTION not in config:
        raise ConfigError(f"[{CFG_SECTION}] section missing")

    kcconfig = config[CFG_SECTION]
    for required_option in ("server", "authtype", "included_tags"):
        if not kcconfig.get(required_option):
            raise ConfigError(f"{required_option} not provided or empty")

    if not kcconfig["server"].startswith("https://"):
        raise ConfigError("server is not an HTTPS URL")
    if not kcconfig["server"].endswith("/kojihub"):
        raise ConfigError("server is not a koji-hub XMLRPC endpoint (/kojihub)")

    if kcconfig["authtype"] == "ssl":
        if not kcconfig.get("cert"):
            raise ConfigError(
                "cert not provided or empty for ssl authtype; "
                "specify cert or switch to gssapi authtype"
            )
    elif kcconfig["authtype"] == "gssapi":
        if not kcconfig.get("principal"):
            raise ConfigError(
                "principal not provided or empty for gssapi authtype; "
                "specify principal or switch to ssl authtype"
            )
    else:
        raise ConfigError("authtype is not 'ssl' or 'gssapi'")


def get_boolean_option(name: str, args: Namespace, config: ConfigParser) -> bool:
    """
    Gets the value of a boolean config file option, which can also be
    turned on by a command-line argument of the same name.
    Raise ConfigError if the option is not a boolean.
    """
    try:
        cmdarg = getattr(args, name, None)
        if cmdarg is None:
            return config[CFG_SECTION].getboolean(name, fallback=False)
        else:
            return bool(cmdarg)
    except ValueError:
        raise ConfigError(f"'{name}' must be a boolean")


def setup_logging(args: Namespace, config: ConfigParser) -> None:
    """
    Sets up logging, given the config and the command-line arguments.

    Logs are written to a logfile if one is defined. In addition,
    log to stderr if it's a tty.
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
            maxBytes=LOG_MAX_SIZE,
            backupCount=1,
        )
        rfh.setLevel(loglevel)
        rfhformatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s",
        )
        rfh.setFormatter(rfhformatter)
        _log.addHandler(rfh)


def parse_command_line(argv: List[str]) -> Namespace:
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

    return parser.parse_args(argv[1:])


def main(argv: Optional[List[str]] = None) -> int:
    """
    Main function.  Get options from the command line and the config;
    set up the necessary objects, then download the list of tags from
    Koji, match them against the patterns listed in the config, and,
    if not in dry-run mode, queue up regen-repo actions.
    """
    global _debug

    #
    # get and handle args and config
    #

    args = parse_command_line(argv or sys.argv)
    config_path: str = args.config

    config = ConfigParser()
    config.read(config_path)
    validate_config(config)

    _debug = get_boolean_option("debug", args, config)
    dry_run: bool = args.dry_run
    wait = get_boolean_option("wait", args, config)
    continue_on_failure = get_boolean_option("continue_on_failure", args, config)
    included_tags = config[CFG_SECTION]["included_tags"].split()

    setup_logging(args, config)
    kojicron = KojiCron(config_path, config_section=CFG_SECTION)
    if not dry_run:
        kojicron.verify_auth()

    #
    # do actual work
    #

    _log.info("kojicron starting")

    tags_to_regen = kojicron.get_tags_to_regen(included_tags)
    if not tags_to_regen:
        raise ProgramError(
            ERR_NO_TAGS_TO_REGEN,
            f"No tags in Koji match the given patterns. Patterns are: {included_tags}",
        )

    if dry_run:
        print("Would regen the following tags:\n" + "\n".join(sorted(tags_to_regen)))

    else:
        failed_tags = kojicron.regen_tags(tags_to_regen, continue_on_failure, wait)

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
