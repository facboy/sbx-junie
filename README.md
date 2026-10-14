# sbx-junie

My fork(ish) of https://github.com/docker/sbx-kits-contrib/tree/main/junie

Uses your user junie install, allows connection to IDE running on the host.

The host side of that connection is kept at the root, outside the kit: `bin/junie-ide-relayd` is the
daemon, and `systemd/user/junie-ide-relayd.service` a sample user unit for running it. See
[`kit/README.md`](kit/README.md#connecting-to-an-ide-on-the-host).
