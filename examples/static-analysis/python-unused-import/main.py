"""Small fixture whose runtime behavior is valid but whose lint contract fails."""

import os


def greeting(name: str) -> str:
    """Return a deterministic greeting used by the fixture smoke test."""

    return f"Hello, {name}!"


if __name__ == "__main__":
    print(greeting("AutoFix"))
