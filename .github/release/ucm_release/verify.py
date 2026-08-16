"""Retired registry operation audit module.

The ``registry audit-operations`` command was replaced by an inline jq
audit in ``.github/workflows/_publish-image-member.yml``; this module is
retained only so ``importlib.import_module("ucm_release.verify")`` keeps
succeeding for the loopback registry contract test.
"""
