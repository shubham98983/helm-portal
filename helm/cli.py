import getpass

from helm.auth import hash_password


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m helm.cli")
    parser.add_argument("command", choices=["hash-password"])
    parser.parse_args()
    print(hash_password(getpass.getpass("Password: ")))


if __name__ == "__main__":
    main()
