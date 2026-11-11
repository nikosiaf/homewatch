# Update and back up

## Update

The System page says when a newer release exists (it checks GitHub once a day, or when you press
Check now). With the updater installed, press **Update to x.y.z** under Health. A page shows what
happens and the release notes; after **OK, update now** a progress page follows the update and opens
the Status page when the new release answers. After 5 minutes without an answer it says the update
failed.

The updater is a systemd unit on the host (`homewatch-update.path`) that runs `update.sh`. homeWatch
gets no Docker access for it: the button only leaves a request file in `update/ask/`. The script
compares that file's first line with a few fixed words (`grafana on`, `grafana off`, `retention 1y`,
`180d`, `90d`, `30d`, from the System page); anything else means "update". It asks GitHub for the latest
release itself, sets `HW_VERSION` in `.env`, then pulls and restarts. If the download fails, or the new release isn't healthy within 3 minutes,
it puts `.env` back and starts the previous release again. It reports to `update/status`, which the
System page shows. It doesn't update `docker-compose.yml`, `Caddyfile` or `update.sh`: when a release
changes them, its release notes say so. Then download them from the release next to `install.sh`, run
`docker compose up -d`, and with the updater `sudo ./update.sh` once (it tells homeWatch what it can do). To add the updater to an existing install, put `update.sh` from the release next to
`install.sh` and run `./install.sh` again (it keeps `.env` and the certificate). To remove it:
`sudo systemctl disable --now homewatch-update.path`.

Without the updater, set `HW_VERSION` in `.env` to the new release, then run:

```sh
docker compose pull && docker compose up -d
```

Database changes apply on start.

## Back up

Everything homeWatch knows lives in two Docker volumes:
- `homewatch_hw-data`: settings, users, history, encrypted tokens, MikroTik configuration copies (and the nightly exports)
- `homewatch_vm-data`: metrics

With Grafana, also `homewatch_grafana-data` (your own dashboards; the built-in ones are recreated).

**Configuration versions** (System page): every saved change to modules, thresholds, timing, channels
or maintenance windows is kept as a version (last 200), showing who changed what. Each can be viewed
and restored; a restore is itself a version, so it can be undone. Passwords, tokens and users are
never part of a version.

**Settings export:** System → Export and import downloads all settings as one YAML file (modules,
thresholds, timing, channels, maintenance windows; no passwords, tokens or users). homeWatch also
writes one every night into the data volume (`exports/`, last 14 kept). Importing a file replaces
the settings; the previous ones stay under Configuration versions, so an import can be undone.

**Full backup and recovery:** keep a copy of `.env` somewhere other than this host, such as a
password manager. `HW_MASTER_KEY` decrypts every stored password and token. Without it the data still
opens, but modules show `WrongMasterKey` until you enter each credential again.

Run these in the folder that holds `docker-compose.yml`. The backup is encrypted with a passphrase of
your choice, because it holds users, history and the encrypted secrets. It needs `gpg`
(`sudo apt install gnupg` on Debian):

```
docker compose stop controller
docker run --rm -v homewatch_hw-data:/data:ro alpine tar czf - -C /data . | gpg -c -o homewatch-data.tar.gz.gpg
docker compose start controller
```

To restore, on the same or a new host, with the original `.env` in place:

```
docker compose create
gpg -d homewatch-data.tar.gz.gpg | docker run --rm -i -v homewatch_hw-data:/data alpine tar xzf - -C /data
docker compose up -d
```

Metrics (`homewatch_vm-data`) can be copied the same way, with `victoriametrics` stopped. Without them,
charts start empty but settings and history are intact.

---

[← homeWatch README](../README.md) · [All documentation](README.md)
