#!/bin/sh
# Keep /dev/usb/hiddevN in sync with the dev_t the kernel actually registers.
#
# ups_monitor only knows how to talk to the UPS through /dev/usb/hiddevN.  When
# the cable is re-seated the kernel can hand the next hiddev instance a
# different minor than the node udev leaves in /dev/usb, and opening the stale
# node fails with ENODEV.  The vendor daemon then starts, zeroes shared memory,
# and keeps running blind -- lsusb still shows 0925:1234, so nothing looks wrong
# except that every reading is zero.  Rebuilding the nodes from sysfs costs
# milliseconds and makes a re-seat recoverable without touching the service.
changed=0
for dir in /sys/class/usbmisc/hiddev*; do
    [ -e "$dir" ] || continue
    name=$(basename "$dir")
    dev=$(cat "$dir/dev" 2>/dev/null) || continue
    case "$dev" in
        *:*) ;;
        *) continue ;;
    esac
    major=${dev%%:*}
    minor=${dev##*:}
    node=/dev/usb/$name
    want=$(printf '%x:%x' "$major" "$minor")
    have=$(stat -c '%t:%T' "$node" 2>/dev/null)
    if [ "$have" != "$want" ]; then
        rm -f "$node"
        mknod "$node" c "$major" "$minor" && chmod 644 "$node"
        echo "hiddev: rebuilt $node as $major:$minor (was ${have:-missing})"
        changed=1
    fi
done
[ "$changed" = 1 ] && udevadm settle --timeout=5
exit 0
