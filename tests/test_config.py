"""Settings, from the environment or a config file.

These tests came over from the Go side with the package, and they are the
definition of what the precedence rule means: the environment beats the file,
the file beats the built-in default, and a bad file is refused rather than
half-applied.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

from bothy import config
from bothy.errors import ConfigError


class ConfigTestCase(unittest.TestCase):
    """Go's t.Setenv, which sets a variable and puts it back afterwards.

    Every test here reads the environment, so one that left a variable behind
    would decide what the next test sees.
    """

    def set_env(self, key, value):
        old = os.environ.get(key)
        if old is None:
            self.addCleanup(os.environ.pop, key, None)
        else:
            self.addCleanup(os.environ.__setitem__, key, old)
        os.environ[key] = value

    def use_fallback(self, f):
        self.addCleanup(config.set_fallback, None)
        config.set_fallback(f)

    def write_config(self, content):
        """Write content to a file whose lifetime is this test's."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "config")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return path


class TestStrFallsBackWhenUnsetOrBlank(ConfigTestCase):
    def test_text_falls_back_when_unset_or_blank(self):
        config.set_fallback(None)
        self.set_env("BOTHY_TEST_STR", "")
        self.assertEqual(
            config.text("BOTHY_TEST_STR", "fallback"),
            "fallback",
            "text on an empty value should be the fallback",
        )
        self.set_env("BOTHY_TEST_STR", "  value  ")
        self.assertEqual(config.text("BOTHY_TEST_STR", "fallback"), "value", "text should trim its value")


class TestIntFallsBackOnNonsense(ConfigTestCase):
    def test_int_falls_back_on_nonsense(self):
        config.set_fallback(None)
        self.set_env("BOTHY_TEST_INT", "not a number")
        self.assertEqual(config.int_("BOTHY_TEST_INT", 7), 7, "int on nonsense should be the default")
        self.set_env("BOTHY_TEST_INT", "12")
        self.assertEqual(config.int_("BOTHY_TEST_INT", 7), 12)


class TestIntFallsBackWhenUnset(ConfigTestCase):
    def test_int_falls_back_when_unset(self):
        config.set_fallback(None)
        self.set_env("BOTHY_TEST_INT_UNSET", "")
        self.assertEqual(
            config.int_("BOTHY_TEST_INT_UNSET", 4), 4, "int on an unset variable should be the default 4"
        )


class TestBoolAcceptsTheSpellingsPeopleUse(ConfigTestCase):
    # These defaults end up in compose files and shell exports, where yes/no and
    # on/off are what people write. A typo must fall back rather than quietly flip a
    # setting off, which is the failure that matters: silently disabling a limit.
    def test_bool_accepts_the_spellings_people_use(self):
        config.set_fallback(None)
        cases = [
            ("", True, True),
            ("", False, False),
            ("true", False, True),
            ("TRUE", False, True),
            ("1", False, True),
            ("yes", False, True),
            ("on", False, True),
            ("false", True, False),
            ("0", True, False),
            ("no", True, False),
            ("off", True, False),
            ("nonsense", True, True),
            ("nonsense", False, False),
        ]
        for value, def_, want in cases:
            with self.subTest(value=value, default=def_):
                self.set_env("BOTHY_TEST_BOOL", value)
                self.assertEqual(
                    config.bool_("BOTHY_TEST_BOOL", def_), want, "Bool(%r, %r)" % (value, def_)
                )


class TestDurParsesDurationsAndFallsBackOnNonsense(ConfigTestCase):
    # Dur backs every timeout and heartbeat in the project, and the defaults it
    # falls back to are what a typo silently keeps: a window of "1h30m" is valid, and
    # "1hr" must not become zero.
    def test_dur_parses_durations_and_falls_back_on_nonsense(self):
        config.set_fallback(None)
        cases = [
            ("", 20.0, 20.0),
            ("90s", 60.0, 90.0),
            ("1h30m", 60.0, 90 * 60.0),
            (" 250ms ", 1.0, 0.25),
            ("0", 60.0, 0.0),
            ("1hr", 60.0, 60.0),
            ("soon", 60.0, 60.0),
            # A parser reports what the text says. A duration that cannot be
            # meaningful for its setting is refused where it is used, by name --
            # see host.New's heartbeat check -- because an env var and a flag
            # arrive by different routes and only one of them goes through here.
            ("-5s", 60.0, -5.0),
        ]
        for value, def_, want in cases:
            with self.subTest(value=value):
                self.set_env("BOTHY_TEST_DUR", value)
                self.assertEqual(config.dur("BOTHY_TEST_DUR", def_), want, "Dur(%r)" % value)


class TestListSplitsAndDropsEmpties(ConfigTestCase):
    def test_list_splits_and_drops_empties(self):
        config.set_fallback(None)
        self.set_env("BOTHY_TEST_LIST", " a , b ,, c ")
        self.assertEqual(config.list_("BOTHY_TEST_LIST"), ["a", "b", "c"])


class TestDurationSyntax(ConfigTestCase):
    # Go's time.ParseDuration has no Python equivalent in the standard library, so
    # the parser is ours and this is the list of what it accepts: a signed sequence
    # of decimal numbers with units, where a fraction is written with a period.
    def test_parse_duration_accepts_go_syntax(self):
        cases = {
            "0": 0.0,
            "10m": 600.0,
            "1h30m": 5400.0,
            "500ms": 0.5,
            "1h": 3600.0,
            "1.5s": 1.5,
            "-5s": -5.0,
            "+90s": 90.0,
            "100us": 0.0001,
            "100\u00b5s": 0.0001,  # the micro sign, as Go accepts it
            "100\u03bcs": 0.0001,  # the Greek mu, which Go accepts too
            "150ns": 150e-9,
            "1.5h": 5400.0,
            "2m30.5s": 150.5,
            "1h2m3s": 3723.0,
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertAlmostEqual(config.parse_duration(raw), want, places=9)

    def test_parse_duration_refuses_what_go_refuses(self):
        # Silently returning zero for a typo would disable the limit the setting
        # exists to impose, so every one of these is an error rather than a default.
        for raw in ["", "1hr", "soon", "10", "s", "--5s", "1h 30m", "00", ".s", "-.s", "1h30", "nan"]:
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError):
                    config.parse_duration(raw)

    def test_format_duration_produces_go_shapes(self):
        cases = [
            (0.0, "0s"),
            (20.0, "20s"),
            (60.0, "1m0s"),
            (90.0, "1m30s"),
            (5400.0, "1h30m0s"),
            (-5.0, "-5s"),
            (0.5, "500ms"),
            (0.25, "250ms"),
            (1.5, "1.5s"),
            (0.0001, "100\u00b5s"),
            (150e-9, "150ns"),
            (3723.0, "1h2m3s"),
        ]
        for seconds, want in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(config.format_duration(seconds), want)

    def test_durations_round_trip(self):
        # A host logs a limit and a health check reports it, so what is printed has
        # to be something the same parser reads back.
        for raw in ["10m", "1h30m", "500ms", "1h", "2m30.5s", "-5s", "90s"]:
            with self.subTest(raw=raw):
                self.assertAlmostEqual(
                    config.parse_duration(config.format_duration(config.parse_duration(raw))),
                    config.parse_duration(raw),
                    places=9,
                )


class TestAMissingFileIsNotAnError(ConfigTestCase):
    # Most people have no config file, and a host that refused to start because a
    # path it was told about did not exist would be worse than one using defaults.
    def test_a_missing_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = config.load(os.path.join(tmp, "not-here"))
        self.assertEqual(f.len(), 0, "want an empty config")


class TestLoadReadsSettings(ConfigTestCase):
    def test_load_reads_settings(self):
        path = self.write_config(
            "\ufeff# a byte-order mark from a Windows editor\n"
            "\n"
            "BOTHY_MAX_CONCURRENT = 2\n"
            "  bothy_peer_quota=200/1h  \n"
            "; also a comment\n"
            "BOTHY_MAX_REQUEST_TIME = 10m   # long generations are allowed, forever is not\n"
            'BOTHY_PUBLIC_ADDRESS = "box.example:7777"\n'
        )
        f = config.load(path)
        self.assertEqual(f.len(), 4, "want 4 settings, got %r" % (f.keys(),))
        self.assertEqual(f._values["BOTHY_MAX_CONCURRENT"], "2")
        self.assertEqual(
            f._values["BOTHY_PEER_QUOTA"], "200/1h", "a lower-case setting name was not understood"
        )
        # The inline comment is not part of the value, or the setting is silently
        # ignored when it fails to parse.
        self.assertEqual(f._values["BOTHY_MAX_REQUEST_TIME"], "10m", "inline comment leaked into the value")
        self.assertEqual(f._values["BOTHY_PUBLIC_ADDRESS"], "box.example:7777", "quotes were not stripped")


class TestAHashInsideAValueSurvives(ConfigTestCase):
    # A # inside a value is not a comment. A share key is allowed to contain one,
    # and a comment is not allowed to eat it.
    def test_a_hash_inside_a_value_survives(self):
        f = config.load(self.write_config("BOTHY_SHARE_KEY = a#b#c\n"))
        self.assertEqual(f._values["BOTHY_SHARE_KEY"], "a#b#c", "want the share key intact")


class TestLoadRefusesASettingNobodyHas(ConfigTestCase):
    # The file exists to say what the limits are, so a setting that configures
    # nothing is worse than a refused start.
    def test_load_refuses_a_setting_nobody_has(self):
        path = self.write_config("BOTHY_MAX_CONCURENT = 2\n")
        with self.assertRaises(ConfigError) as raised:
            config.load(path)
        message = str(raised.exception)
        self.assertIn(
            "BOTHY_MAX_CONCURRENT", message, "the error should name the setting that was meant"
        )
        self.assertIn("line 1", message, "the error should say which line")


class TestLoadRefusesALineThatIsNotASetting(ConfigTestCase):
    def test_load_refuses_a_line_that_is_not_a_setting(self):
        path = self.write_config("BOTHY_MAX_CONCURRENT 2\n")
        with self.assertRaises(ConfigError):
            config.load(path)

    def test_load_refuses_a_line_with_no_name(self):
        path = self.write_config("= 2\n")
        with self.assertRaises(ConfigError) as raised:
            config.load(path)
        self.assertIn("line 1", str(raised.exception))


class TestEnvironmentBeatsFileBeatsDefault(ConfigTestCase):
    # The whole precedence rule in one test: a flag beats the environment, the
    # environment beats the file, the file beats the built-in default. Flags are
    # tested where they are parsed; this covers the other three.
    def test_environment_beats_file_beats_default(self):
        f = config.load(
            self.write_config(
                "BOTHY_MAX_CONCURRENT = 7\n"
                "BOTHY_PAUSED = yes\n"
                "BOTHY_MAX_REQUEST_TIME = 90s\n"
                "BOTHY_MAX_BODY = 1024\n"
            )
        )
        self.use_fallback(f)

        self.assertEqual(config.int_("BOTHY_MAX_CONCURRENT", 4), 7, "from the file")
        self.assertEqual(config.int_("BOTHY_OWNER_RESERVE", 1), 1, "unset: want the built-in default 1")
        self.assertEqual(config.dur("BOTHY_MAX_REQUEST_TIME", 0.0), 90.0, "from the file")
        self.assertEqual(config.int64("BOTHY_MAX_BODY", 0), 1024, "from the file")
        # The spellings a person actually writes.
        self.assertTrue(config.bool_("BOTHY_PAUSED", False), "from the file: yes should mean true")

        # A container overrides a machine's standing policy, which is what makes the
        # devnet and the compose files work.
        self.set_env("BOTHY_MAX_CONCURRENT", "9")
        self.assertEqual(config.int_("BOTHY_MAX_CONCURRENT", 4), 9, "the environment did not beat the file")


class TestKeysAreSortedAndPathIsKept(ConfigTestCase):
    # `bothy config path` and a startup line both print these, so they have to be
    # the settings that were actually set rather than what the file contains.
    def test_keys_are_sorted_and_the_path_is_kept(self):
        path = self.write_config("BOTHY_PAUSED = true\nBOTHY_MAX_BODY = 1\n")
        f = config.load(path)
        self.assertEqual(f.keys(), ["BOTHY_MAX_BODY", "BOTHY_PAUSED"])
        self.assertEqual(f.path(), path)


class TestDefaultPath(ConfigTestCase):
    def test_default_path_is_the_os_per_user_config_dir(self):
        home = "C:\\Users\\someone" if os.name == "nt" else "/home/someone"
        if os.name == "nt":
            environ = {"APPDATA": os.path.join(home, "AppData", "Roaming")}
            want = os.path.join(home, "AppData", "Roaming", "bothy", "config")
        elif sys.platform == "darwin":
            environ = {"HOME": home}
            want = os.path.join(home, "Library", "Application Support", "bothy", "config")
        else:
            environ = {"HOME": home, "XDG_CONFIG_HOME": ""}
            want = os.path.join(home, ".config", "bothy", "config")
        with mock.patch.dict(os.environ, environ):
            if os.name != "nt":
                os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(config.default_path(), want)

    def test_default_path_falls_back_when_the_os_has_no_config_dir(self):
        # Better a surprising path than no config at all: the caller prints it, and
        # `bothy config path` is the answer to "where did that come from?".
        keys = ["APPDATA"] if os.name == "nt" else ["XDG_CONFIG_HOME", "HOME"]
        with mock.patch.dict(os.environ):
            for key in keys:
                os.environ.pop(key, None)
            self.assertEqual(config.default_path(), "bothy.conf")


class TestARetiredSettingIsStillAccepted(ConfigTestCase):
    # A setting Bothy no longer has still loads, so that upgrading does not break a
    # host whose config file mentions it. The task runner's directory is the case:
    # it was retired, and a file that names it is read rather than refused.
    def test_a_retired_setting_does_not_break_the_file(self):
        f = config.load(self.write_config("BOTHY_SWARM_DIR = /srv/swarm\nBOTHY_MODELS_DIR = /models\n"))
        self.assertEqual(f.keys(), ["BOTHY_MODELS_DIR", "BOTHY_SWARM_DIR"])
        self.assertIn("BOTHY_SWARM_DIR", config.KNOWN_KEYS)


class TestListenDefaultHonoursAPlatformPort(ConfigTestCase):
    # A container platform tells a process which port its traffic arrives on
    # through $PORT, and a service that ignores it is a service the platform never
    # routes to. It is a hint rather than a setting: anything written down for
    # Bothy itself wins, which is the precedence rule the rest of this module uses.
    def test_the_platform_port_is_used_when_nothing_else_says(self):
        self.set_env("PORT", "9090")
        self.assertEqual(config.listen_default(":8080"), ":9090")

    def test_a_setting_beats_the_platform_port(self):
        self.set_env("PORT", "9090")
        self.set_env("BOTHY_LISTEN", "127.0.0.1:1234")
        self.assertEqual(config.listen_default(":8080"), "127.0.0.1:1234")

    def test_the_named_keys_are_tried_in_order(self):
        self.set_env("PORT", "9090")
        self.set_env("BOTHY_LISTEN", ":1111")
        self.set_env("BOTHY_HOST_LISTEN", ":2222")
        self.assertEqual(config.listen_default(":7777", "BOTHY_HOST_LISTEN", "BOTHY_LISTEN"), ":2222")

    def test_the_command_keeps_its_own_default_without_any_of_them(self):
        self.set_env("PORT", "")
        self.assertEqual(config.listen_default("127.0.0.1:11223"), "127.0.0.1:11223")


if __name__ == "__main__":
    unittest.main()
