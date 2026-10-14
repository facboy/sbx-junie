#!/bin/sh

mkdir -p ~/.local/share/
/usr/bin/ln -s "${USER_HOME}/.local/share/junie" ~/.local/share/

mkdir -p ~/.local/bin/
/usr/bin/ln -s "${USER_HOME}/.local/bin/junie" ~/.local/bin
