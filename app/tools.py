"""Jobs for whoever runs the platform, from its folder:

    .venv/bin/python -m app.tools reset-link you@example.com    a link to choose a new password (works for 24 hours)
"""
import sys

from . import create_app


def main(argv):
    if len(argv) != 2 or argv[0] != "reset-link":
        print(__doc__)
        return 1
    app = create_app({"WORKER": False})
    with app.app_context():
        from .models import User
        from .web.auth import reset_link
        user = User.query.filter(User.email == argv[1].strip().lower()).first()
        if user is None:
            print(f"There's no account for {argv[1]}.")
            return 1
        print(reset_link(user))  # a link needs the platform's context, not a request
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
