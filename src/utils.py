# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""A collection of utility functions that are used in the charm."""

import hashlib
import secrets
import string


def generate_random_password(length: int) -> str:
    """Randomly generate a string intended to be used as a password.

    Args:
        length: length of the randomly generated string to be returned

    Returns:
        a string with random letters and digits of length specified
    """
    choices = string.ascii_letters + string.digits
    return "".join([secrets.choice(choices) for i in range(length)])


def generate_random_hash() -> str:
    """Generate a random hash.

    Returns:
        A random MD5 hash
    """
    random_characters = generate_random_password(20)
    return hashlib.md5(random_characters.encode("utf-8")).hexdigest()
