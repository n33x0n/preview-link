---
name: preview-link
description: Use when a task needs a file at a public link (a review copy of a page, a video, any short-lived preview), or when the nginx vhost that serves previews must be set up or checked. Publishes, checks and deletes files with preview-link.
---

# preview-link

Public, short-lived files behind an unguessable URL. nginx serves `<root>/<20-hex-token>/<file>`. The token is not authentication: anyone with the link can read the file.

## Commands

```sh
preview-link put page.html                 # prints the public link
preview-link put page.html --over <link>   # replaces the file, the link stays the same
preview-link check <link>                  # status and Content-Type, as an outside client gets them
preview-link rm <link>                     # deletes the file and confirms the link returns 404
preview-link ls                            # what is still public
preview-link doctor                        # checks nginx, the vhost and a real round trip
```

`put --name NAME` publishes under another file name. Names use letters, digits, dot, dash and underscore.

If `put` says the tool is not configured, nothing has run `init` on this machine yet. See "Set up" below.

## Rules

1. Run `check` before you send a link. You need status 200 and the right content type.
2. Delete each preview when its review is done. `rm` must report 404. If the same link must show a changed file, use `put --over`, then `check` it.
3. Never put a preview in a repo, an issue or a chat attachment. Those cannot be taken down.
4. Never publish secrets, tokens or private data. Anyone with the link can read it.
5. Tell the user that copies made by recipients or caches may outlive `rm`.

## Set up

Do this only when the user asks for it or agrees to it.

1. Run `preview-link init --host HOST` without `--apply`. This is a dry run. It prints the detected system, the plan, the full vhost and the commands. It changes nothing.
2. Show the plan to the user. Ask which host name, root directory and TLS mode they want. Do not guess a host name.
3. Run the same command again with `--apply` only after the user says yes. It needs root for `/etc/nginx`. Never run `sudo` yourself; tell the user which step needs root.
4. Run `preview-link doctor`. Every line must say `ok`.
5. For `--tls certbot`, the tool only prints the `certbot` command. The user runs it.

Never add `--install-nginx` without the user's explicit approval. It installs a system package as root. Never add `--force` unless the user has seen which file it would replace. If `init` stops because it cannot prove that nginx includes the target directory, show the user the `include` line it prints. Do not edit `nginx.conf` yourself.

To remove the vhost: `preview-link init --undo HOST` (dry run), then with `--apply` after the user agrees. Published files stay unless the user also approves `--purge`.

Settings live in `${XDG_CONFIG_HOME:-~/.config}/preview-link/config.json`. The variables `PREVIEW_LINK_CONFIG`, `PREVIEW_LINK_ROOT`, `PREVIEW_LINK_URL` and `PREVIEW_LINK_LOG` override the defaults. Actions are logged to `${XDG_STATE_HOME:-~/.local/state}/preview-link/log.jsonl`.
