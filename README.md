# kojicron

Script for performing Koji tasks periodically. The current available task
is to run `regen-repo` on tags matching one or more globs.

## Installation instructions

1.  Clone the repository into `/usr/local/src/kojicron`.
2.  Create `/etc/kojicron`.
3.  Put `kojicron.conf` into `/etc/kojicron/kojicron.conf`.  Edit the parameters as desired.
4.  Set up the `kojicron` user:
    1.  Create a `.pem` file (a concatenated cert+key) for the user, and put it into `/etc/kojicron/kojicron.pem`,
        owned by `root:root`, `0600`.
    2.  Create a `kojicron` user in Koji, authenticated by SSL.
    3.  Grant the `kojicron` user `repo` permission.
5.  Copy `kojicron.service` and `kojicron.timer` into `/etc/systemd/system/`.
    Edit the files as desired.
6.  Run:
    1.  `systemctl daemon-reload`
    2.  `systemctl enable --now kojicron.timer`
