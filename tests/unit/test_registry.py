import unittest

from sut.registry import (
    MAX_MEMORY_MB, MAX_TIMEOUT_S, Conflict, NotFound, Registry, ValidationError,
    spec_from_dict, validate,
)
from tests.support import spec


class TestValidate(unittest.TestCase):
    def test_default_spec_is_valid(self):
        self.assertEqual(validate(spec()), [])

    def test_rejects_bad_names(self):
        for name in ["", "ab", "Hello", "1abc", "abc-", "a" * 33, "has_underscore"]:
            with self.subTest(name=name):
                self.assertTrue(validate(spec(name=name)))

    def test_accepts_boundary_name_lengths(self):
        self.assertEqual(validate(spec(name="abc")), [])
        self.assertEqual(validate(spec(name="a" * 32)), [])

    def test_rejects_unknown_runtime_and_handler(self):
        errors = validate(spec(runtime="cobol", handler="nope"))
        self.assertEqual(len(errors), 2)

    def test_memory_bounds_are_inclusive(self):
        self.assertEqual(validate(spec(memory_mb=64)), [])
        self.assertEqual(validate(spec(memory_mb=MAX_MEMORY_MB)), [])
        self.assertTrue(validate(spec(memory_mb=0)))
        self.assertTrue(validate(spec(memory_mb=MAX_MEMORY_MB + 64)))

    def test_memory_must_be_multiple_of_64(self):
        self.assertIn("memory_mb must be a multiple of 64", validate(spec(memory_mb=100)))

    def test_timeout_bounds(self):
        self.assertEqual(validate(spec(timeout_s=1)), [])
        self.assertEqual(validate(spec(timeout_s=MAX_TIMEOUT_S)), [])
        self.assertTrue(validate(spec(timeout_s=0)))
        self.assertTrue(validate(spec(timeout_s=MAX_TIMEOUT_S + 1)))

    def test_replica_bounds(self):
        self.assertTrue(validate(spec(min_replicas=-1)))
        self.assertTrue(validate(spec(max_replicas=0)))
        self.assertTrue(validate(spec(min_replicas=3, max_replicas=2)))
        self.assertEqual(validate(spec(min_replicas=2, max_replicas=2)), [])

    def test_target_concurrency_positive(self):
        self.assertTrue(validate(spec(target_concurrency=0)))


class TestSpecFromDict(unittest.TestCase):
    def test_round_trip(self):
        s = spec(memory_mb=256)
        self.assertEqual(spec_from_dict(s.to_dict()), s)

    def test_rejects_bool_as_int(self):
        with self.assertRaises(ValidationError):
            spec_from_dict({"name": "abc", "runtime": "go1.22", "handler": "echo", "memory_mb": True})

    def test_reports_missing_and_unknown_fields(self):
        with self.assertRaises(ValidationError) as ctx:
            spec_from_dict({"name": "abc", "colour": "red"})
        msg = str(ctx.exception)
        self.assertIn("missing field 'runtime'", msg)
        self.assertIn("unknown fields: colour", msg)

    def test_rejects_non_object(self):
        with self.assertRaises(ValidationError):
            spec_from_dict(["not", "a", "dict"])


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = Registry()

    def test_create_then_get(self):
        self.assertEqual(self.reg.create(spec()), 1)
        got, version = self.reg.get("hello")
        self.assertEqual((got.name, version), ("hello", 1))

    def test_duplicate_create_conflicts(self):
        self.reg.create(spec())
        with self.assertRaises(Conflict):
            self.reg.create(spec())

    def test_update_bumps_version(self):
        self.reg.create(spec())
        self.assertEqual(self.reg.update(spec(memory_mb=256)), 2)
        self.assertEqual(self.reg.update(spec(memory_mb=512)), 3)
        self.assertEqual(self.reg.get("hello")[0].memory_mb, 512)

    def test_update_missing_raises(self):
        with self.assertRaises(NotFound):
            self.reg.update(spec())

    def test_invalid_spec_not_stored(self):
        with self.assertRaises(ValidationError):
            self.reg.create(spec(memory_mb=1))
        self.assertEqual(len(self.reg), 0)

    def test_delete(self):
        self.reg.create(spec())
        self.reg.delete("hello")
        with self.assertRaises(NotFound):
            self.reg.get("hello")
        with self.assertRaises(NotFound):
            self.reg.delete("hello")

    def test_names_sorted(self):
        for n in ["zeta", "alpha", "mid"]:
            self.reg.create(spec(name=n))
        self.assertEqual(self.reg.names(), ["alpha", "mid", "zeta"])


if __name__ == "__main__":
    unittest.main()
