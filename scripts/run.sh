#!/usr/bin/env bash

session_name=PiKaraoke
cmds=(
#"top"
#"sudo sh -c 'cp -f $HOME/.Xauthority ~ && PATH=$PATH python3 app.py -u $(whoami)'"
#"PATH='$PATH' ./screencapture.sh -v -p 4000"
"python3 vocal_splitter.py -d ~/pikaraoke-songs/ -rd /mnt/ramdisk"
"DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY SDL_VIDEODRIVER=x11 python3 app.py --show-overlay -w -nv --admin-password admin --temp /mnt/ramdisk; tmux kill-session -t $session_name"
#"pavucontrol"
)

if [ "`tmux ls | grep $session_name`" ]; then
	echo "TMUX Session $session_name already exists!" >&2
	exit 1
fi

export DISPLAY=:0

cd "`dirname $0`"
tmux new-session -s $session_name -d -x 240 -y 60
tmux set-environment -t $session_name DISPLAY "$DISPLAY"
tmux set-environment -t $session_name XAUTHORITY "$XAUTHORITY"

for i in `seq 0 $[${#cmds[*]}-1]`; do
	if [ $i -gt 0 ]; then
		sleep 0.2
		tmux split-window
		sleep 0.2
		tmux select-layout even-horizontal
		sleep 0.2
	fi
	tmux send-keys -l "${cmds[i]}"
	sleep 0.2
	tmux send-keys Enter
done

# Set pulseaudio recording source
#src="`pacmd list-sources | grep '.monitor>' | awk '{print $2}' | head -1 `"
#idx="`pacmd list-source-outputs | grep index: | awk '{print $2}' | tail -1`"
#if [ "$src" ]; then
#	pacmd move-source-output $idx "${src:1:-1}"
#fi

tmux a -t $session_name

