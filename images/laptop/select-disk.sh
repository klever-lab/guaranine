#!/bin/sh
# d-i otherwise silently chooses the only disk at critical priority.
# shellcheck disable=SC2119
set -eu
. /usr/share/debconf/confmodule
debconf-loadtemplate guaranine /cdrom/guaranine/disk.templates
choices=''
details=''
media=$(readlink -f /dev/disk/by-label/GUARANINE || true)
for disk in $(list-devices disk); do
    case "$media" in "$disk"|"$disk"[0-9]*|"${disk}p"[0-9]*) continue ;; esac
    choices="${choices:+$choices, }$disk"
    block=${disk##*/}
    sectors=$(cat "/sys/class/block/$block/size")
    details="$details $disk ($((sectors / 2097152)) GiB);"
done
test -n "$choices"
db_subst guaranine/disk DISKS "$choices"
db_subst guaranine/disk DETAILS "$details"
while :; do
    db_fset guaranine/disk seen false
    db_input critical guaranine/disk
    db_go
    db_get guaranine/disk
    selected=$RET
    # Reject an empty/forged answer and never seed a default target disk.
    case ", $choices, " in *", $selected, "*) test -n "$selected" ;; *) exit 1 ;; esac
    db_subst guaranine/confirm-disk DISK "$selected"
    db_set guaranine/confirm-disk ''
    db_fset guaranine/confirm-disk seen false
    db_input critical guaranine/confirm-disk
    db_go
    db_get guaranine/confirm-disk
    test "$RET" = "$selected" && break
done
db_set partman-auto/disk "$selected"
db_fset partman-auto/disk seen true
