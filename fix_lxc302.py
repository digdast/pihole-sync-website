import time
import paramiko

s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("192.168.40.34", username="root", password="sone_420", timeout=10)

# 1. Set external nameserver on LXC 302 so gravity/OS DNS never deadlocks when FTL restarts
s.exec_command('pct set 302 -nameserver "1.1.1.1 8.8.8.8"')[1].read()

# 2. Patch /usr/bin/start.sh inside LXC 302 so pihole-FTL automatically restarts in a loop after Teleporter import (exit code 22)
sftp = s.open_sftp()
_, out, _ = s.exec_command("pct exec 302 -- cat /usr/bin/start.sh")
start_sh = out.read().decode("utf-8")

old_line = 'capsh --user="${DNSMASQ_USER}" --keep=1 -- -c "/usr/bin/pihole-FTL $FTL_CMD >/dev/null" &'
new_line = 'capsh --user="${DNSMASQ_USER}" --keep=1 -- -c "while true; do /usr/bin/pihole-FTL $FTL_CMD >/dev/null; sleep 1; done" &'

if old_line in start_sh:
    start_sh = start_sh.replace(old_line, new_line)
    with sftp.open("/tmp/start_302.sh", "w") as f:
        f.write(start_sh)
    s.exec_command("pct push 302 /tmp/start_302.sh /usr/bin/start.sh --perms 0755")[1].read()
    print("[OK] Patched /usr/bin/start.sh in LXC 302 with auto-restart loop.")
elif "while true; do /usr/bin/pihole-FTL" in start_sh:
    print("[OK] /usr/bin/start.sh in LXC 302 already patched.")

sftp.close()

# 3. Restart LXC 302 cleanly
s.exec_command("pct stop 302 && pct start 302")[1].read()
time.sleep(5)
_, out, _ = s.exec_command("pct exec 302 -- ps aux")
print(out.read().decode("utf-8"))
s.close()
