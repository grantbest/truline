from src.routes import (
    _is_missing_parent_error,
    _is_plaid_duplicate_error,
    _is_spec_identity_duplicate_error,
)


class FakeOrig:
    sqlstate = "23505"

    def __str__(self):
        return "duplicate key value violates unique constraint idx_unique_plaid_id"


class FakeIntegrityError(Exception):
    orig = FakeOrig()


def test_plaid_duplicate_error_detection():
    assert _is_plaid_duplicate_error(FakeIntegrityError())


class FakeSpecIdentityOrig:
    sqlstate = "23505"

    def __str__(self):
        return (
            "duplicate key value violates unique constraint "
            '"idx_unique_dev_task_spec_identity_live"'
        )


class FakeSpecIdentityIntegrityError(Exception):
    orig = FakeSpecIdentityOrig()


def test_spec_identity_duplicate_error_detection():
    assert _is_spec_identity_duplicate_error(FakeSpecIdentityIntegrityError())


def test_spec_identity_duplicate_error_does_not_misclassify_plaid():
    """The two checks key on different index names — a plaid violation must
    not also read as a spec_identity violation (OPS-68 relies on this to
    check its own condition first without needing to touch the plaid
    check's broader fallback clause)."""
    assert not _is_spec_identity_duplicate_error(FakeIntegrityError())


class FakeMissingParentOrig:
    sqlstate = "23503"

    def __str__(self):
        return (
            'insert or update on table "bead" violates foreign key constraint '
            '"bead_parent_id_fkey"\nDETAIL:  Key (parent_id)=(deadbeef) is not '
            'present in table "bead".'
        )


class FakeMissingParentIntegrityError(Exception):
    orig = FakeMissingParentOrig()


def test_missing_parent_error_detection():
    assert _is_missing_parent_error(FakeMissingParentIntegrityError())


def test_missing_parent_error_does_not_misclassify_unique_violations():
    """23503 (foreign key) and 23505 (unique) are disjoint SQLSTATEs — a
    duplicate-key error must never also read as a missing parent, in either
    direction, regardless of what order the three checks run in."""
    assert not _is_missing_parent_error(FakeIntegrityError())
    assert not _is_missing_parent_error(FakeSpecIdentityIntegrityError())


def test_duplicate_errors_do_not_misclassify_as_missing_parent():
    assert not _is_plaid_duplicate_error(FakeMissingParentIntegrityError())
    assert not _is_spec_identity_duplicate_error(FakeMissingParentIntegrityError())
