# preview-link

A small command-line tool and agent skill. It publishes a file at a public link with a random token, checks the link as an outside client would, and deletes the file when the review is done. nginx serves the files. The tool can set up a vhost for a chosen host name on supported nginx layouts; see the tested environments and limits below.

It is one Python 3 script with no dependencies beyond the standard library.

## What it does

- `put FILE` copies a file to `<root>/<20-hex-token>/<file>` and prints the public link. `put --over LINK` replaces the file and keeps the link.
- `check LINK` prints the HTTP status and content type, and warns on stderr when a response header is missing.
- `rm LINK` deletes the file only if the link belongs to the configured base URL and names an existing preview. A bare token deletes its preview directory. It then requests each removed link and reports whether it returns 404.
- `ls` lists what is still public.
- `init --host HOST` sets up an nginx vhost for the preview root. It prints a dry-run plan by default and writes only with `--apply`.
- `init --undo HOST` removes the vhost that `init` wrote.
- `doctor` checks nginx, the vhost and a real round trip against the local nginx.
- Folders get mode 0755 and files 0644 explicitly, so a strict `umask` cannot hide a file from nginx.
- Every action goes to a JSON-lines log.
- The vhost sends `Cache-Control: no-store, no-transform`, `X-Robots-Tag: noindex` and `X-Content-Type-Options: nosniff` on every response, serves only GET and HEAD, and lists no directory.

The token is not authentication. Anyone who has the link can read the file. Files do not expire automatically. Use a dedicated preview root: matching token paths under it are public.

## Install

Requirements: `python3` 3.9 or newer, and nginx on the server that serves the files. `init` can print the install command for nginx on your system.

Copy the script onto your `PATH`:

```sh
install -m 0755 preview-link /usr/local/bin/preview-link
```

To use it as an agent skill, copy `SKILL.md` into your agent's skills directory (for example `.omp/skills/preview-link/`, `.claude/skills/preview-link/` or `.agents/skills/preview-link/`). The skill expects the `preview-link` command on `PATH`.

## Usage

### Fresh Debian or Ubuntu server

```sh
preview-link init --host preview.example.org            # dry run: prints plan, vhost and commands
sudo preview-link init --host preview.example.org --install-nginx --apply
preview-link doctor
```

If nginx is not installed, the dry run exits non-zero and prints the `apt-get` commands. `--install-nginx --apply` runs them, but only as root. The tool never calls `sudo` itself.

Point DNS for the host at the server. For HTTPS, see TLS below.

### Server that already runs nginx with other vhosts

```sh
preview-link init --host preview.example.org            # read the detected layout and the plan
sudo preview-link init --host preview.example.org --apply
```

The dry run shows how this nginx loads vhost files and proves that the target directory is included in the `http` block. It never edits `nginx.conf`. If it cannot prove the include, it stops and prints the one `include` line to add. If your nginx includes a directory the tool did not pick, pass it with `--nginx-conf-dir`.

If another server block already uses the same `server_name` and port, the plan shows a warning. nginx keeps the first match and ignores the other.

### Publish, check, delete

```sh
preview-link put page.html                # prints https://preview.example.org/<token>/page.html
preview-link check <link>                 # 200  text/html
preview-link put page.html --over <link>  # same link, new content
preview-link ls
preview-link rm <link>                    # deleted, now 404
```

`put --name NAME` publishes under another file name. Names use letters, digits, dot, dash and underscore, and start with a letter or digit. `put --over LINK` accepts only a link under the configured base URL.

### `init` flags

| Flag | Meaning |
| --- | --- |
| `--host HOST` | Server name of the vhost. Required unless `--undo`. |
| `--root DIR` | Directory for published files. Default `/var/www/preview-link`. |
| `--listen [ADDR:]PORT` | Listen address. Default `80`. Use `127.0.0.1:8080` behind a proxy. |
| `--tls none\|external\|certbot` | See TLS. Default `none`. |
| `--base-url URL` | Prefix of the links `put` prints. Overrides the one `--tls` implies. |
| `--nginx-conf-dir DIR` | Directory nginx includes in its `http` block. Default: detected. |
| `--nginx-bin PATH` | nginx binary. Default: `nginx` on `PATH`, then the usual `sbin` directories. |
| `--install-nginx` | Install nginx with the system package manager. Needs `--apply` and root. |
| `--force` | Replace a vhost file that does not carry the `# managed by preview-link` marker. |
| `--no-reload` | Write and test, but do not reload nginx. |
| `--apply` | Write. Without it `init` only prints. |
| `--undo HOST` | Remove the vhost for HOST. Dry run unless `--apply`. |
| `--purge` | With `--undo`: also delete the published token folders and the empty root. |

What `init` does with `--apply`, in order:

1. Creates the root directory with mode 0755 if it is missing. Existing directories are not changed.
2. Writes `preview-link-<host>.conf` atomically (temporary file, then rename). On Debian style layouts the file goes to `sites-available` and a symlink goes to `sites-enabled`.
3. Runs `nginx -t`. If it fails, it removes the new file and symlink, restores the old content if it replaced a file, removes directories it created, prints the nginx error and exits non-zero.
4. Writes the config file.
5. Reloads nginx with `nginx -s reload`, or `systemctl reload nginx` when you did not pass `--nginx-bin`, `systemctl` exists and the `nginx` unit is active.

It does not chown anything. Files are world-readable, so the nginx worker user can read them. The tool reports the worker user it found with `nginx -T` and warns if a parent directory of the root is not searchable by other users.

### Configuration

`init --apply` writes `${XDG_CONFIG_HOME:-~/.config}/preview-link/config.json` with the root, the base URL, the vhost path and the listen address. If you run `init` with `sudo`, the file goes to the home directory that `sudo` sets. Copy it to the publishing user, or point `PREVIEW_LINK_CONFIG` at it.

| Variable | Meaning |
| --- | --- |
| `PREVIEW_LINK_CONFIG` | Config file path. |
| `PREVIEW_LINK_ROOT` | Root directory. Overrides the config file. |
| `PREVIEW_LINK_URL` | Link prefix. Overrides the config file. |
| `PREVIEW_LINK_LOG` | Log file. Default `${XDG_STATE_HOME:-~/.local/state}/preview-link/log.jsonl`. |
| `PREVIEW_LINK_CONNECT` | `HOST:PORT`. `check` and `rm` send `http://` links to that address and keep the link's `Host` header. Use it to check a vhost before DNS exists. |
| `PREVIEW_LINK_OS_RELEASE` | Alternative to `/etc/os-release`, for tests. |
| `PREVIEW_LINK_EXTRA_PATH` | Extra directories searched for `nginx` and package managers. Default: the usual `sbin` directories. |

There is no default public URL. If nothing is configured, `put` exits with a message that says to run `init`.

One config file holds one vhost. To manage several hosts, use one `PREVIEW_LINK_CONFIG` file per host.

### TLS

- `none` serves plain HTTP. Links start with `http://HOST`, plus `:PORT` when the port is not 80.
- `external` means TLS ends before nginx: a CDN, a tunnel or a load balancer. The vhost still listens for plain HTTP. Links start with `https://HOST`. The front end must pass the `Host` header and the preview paths unchanged.
- `certbot` only prints the command to run next: `certbot --nginx -d HOST`. The tool never runs certbot and never asks for an e-mail address. Links start with `https://HOST` and work after certbot has finished. DNS must point at the server first.

Use `--base-url` when the public address differs from what these modes assume.

### `doctor`

`doctor` prints one line per check and exits non-zero if any fails:

- the nginx binary is present;
- the vhost file exists and carries the marker;
- `nginx -t` passes;
- the root is writable by the current user;
- a probe file is published, fetched from `http://127.0.0.1:PORT` with the vhost's `Host` header, and deleted. It checks status 200, content type, the three headers, that the token folder and the root are not listable, that a dotfile is denied, that `robots.txt` disallows everything, that POST is refused, and that the deleted file returns 404.

### Tests

```sh
sh test/run.sh
```

No root, no network, no change outside a temporary directory. The tests include:

- Detection and plan text, with fake `nginx`, `/etc/os-release`, `apt-get`, `dnf`, `yum`, `apk` and `systemctl` programs: package manager choice for apt, dnf, yum (through `ID_LIKE`), apk and an unknown system; Debian, `conf.d`, `servers`, relative-include and no-include layouts; an include outside the `http` block is not accepted; config read from disk when `nginx -T` fails; TLS modes; bad arguments; duplicate `server_name` warning.
- `init --apply` against fake `nginx`: reload command choice (`nginx -s reload`, `systemctl reload nginx`, `--no-reload`), the Debian symlink, rollback that removes a new file or restores the old content, refusal to write where root is needed, refusal to overwrite without the marker, and `--force`.
- A real, unprivileged nginx in a private prefix on a free port: `init` dry run (directory trees compared before and after), `init --apply`, reload, `doctor`, `put`, `check`, `put --over`, `ls`, `rm`, a strict `umask`, status codes, headers, no listings, dotfile denied, 404 after `rm`, a real `nginx -t` failure (a host name too long for the test config's hash size) that is rolled back, `--force`, a second `init` that reloads, `init --undo` with and without `--purge`, and `doctor` failing after undo.

The test nginx and its temporary directory are removed at the end, also when a test fails. The tests skip the real-nginx part if `/usr/sbin/nginx` is missing, and the `--install-nginx --apply` test only checks the "needs root" path when you do not run it as root.

## What breaks or doesn't work

- The token is not authentication. Anyone with the link can read the file. Do not publish secrets. `rm` removes the file and confirms 404 from where you run it, but a recipient, a cache or a CDN may keep a copy. Nothing expires files by itself.
- Not tested: real package installs (`apt-get`, `dnf`, `yum`, `apk`, `pacman`, `zypper`); only the choice of command is tested, with fake programs. The `pacman`, `zypper` and Homebrew commands are not even covered by that.
- Not tested: the real `systemctl reload nginx` path (only the choice is tested with a fake `systemctl`), certbot (the command is printed, never run), the `.well-known` challenge path with the dotfile rule, Homebrew and macOS, SELinux, firewalls and non-Linux systems. Homebrew-style `servers/` detection is tested with a synthetic config only.
- Not tested: nginx versions other than 1.28.3, and Python versions other than 3.14. The script parses with the 3.9 grammar, but it has never run on 3.9.
- A dry run on one Ubuntu host with a Debian-style `sites-available` layout detected it correctly. `nginx -T` failed there for a non-root user, so the tool read the config from disk. Nothing was written.
- The config reader follows `include` lines with literal paths and wildcards. It does not evaluate variables or unusual constructs. When it cannot prove an include, it stops.
- The vhost listens on IPv4 only (`listen PORT;`). Add an IPv6 listener by hand if you need one.
- `init` needs root to write to `/etc/nginx` and to reload nginx. It never calls `sudo`. As a normal user it stops before changing anything and names the step.
- Directories created by `init` as root belong to root. Publish as root, or run `chown USER DIR` on the root directory.
- `init` runs `nginx -t` on the whole config. If another vhost already makes it fail, `init` rolls back and stops.
- The base URL assumes `--tls` and `--listen`. If a proxy or CDN changes the public host or port, set `--base-url`.
- `check` and `rm` use the system certificate store for `https://` links.

## Files

- `preview-link` — the command-line tool (Python 3, standard library only).
- `SKILL.md` — the agent skill: when to use the tool and how to run `init` safely.
- `test/test_preview_link.py` — the tests. `test/run.sh` runs them.
- `LICENSE` — Apache License 2.0.
- `.gitignore` — ignores Python caches.

## License

Apache License 2.0.
