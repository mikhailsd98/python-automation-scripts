# proxy_v_generator.py
import subprocess, time, requests, os, sys, re
from typing import Optional, Tuple

VPN_NAME = "MySSTP2"
USERNAME = "MikeSD2"
PASSWORD = "07031998Mike"

HOST_PREFIX = "193-118-55-"
HOST_SUFFIX = ".hideservers.net"

def run(cmd):
	"""Run a command via cmd.exe (legacy).

	NOTE: For commands that include Unicode paths (e.g. `C:\\Users\\Пользователь`),
	prefer the *args* helpers below (shell=False) to avoid argument mangling.
	"""
	r = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="cp866", errors="replace")
	out = (r.stdout or "") + (r.stderr or "")
	print(out)
	return r.returncode


def run_capture(cmd) -> Tuple[int, str]:
	"""Run a command via cmd.exe and capture output (legacy)."""
	p = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="cp866", errors="replace")
	out = (p.stdout or "") + (p.stderr or "")
	print(out)
	return p.returncode, out


def _run_capture_silent(cmd) -> Tuple[int, str]:
	p = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="cp866", errors="replace")
	out = (p.stdout or "") + (p.stderr or "")
	return p.returncode, out


def _run_capture_args(cmd_args: list[str]) -> Tuple[int, str]:
	"""Run a command with shell=False (Unicode-safe) and capture output.

	This avoids `cmd.exe` quoting/encoding issues (especially with Cyrillic paths)
	that can make `rasdial` print "USAGE" instead of connecting.
	"""
	p = subprocess.run(cmd_args, shell=False, capture_output=True, text=True, encoding="cp866", errors="replace")
	out = (p.stdout or "") + (p.stderr or "")
	print(out)
	return p.returncode, out


def _run_capture_args_silent(cmd_args: list[str]) -> Tuple[int, str]:
	p = subprocess.run(cmd_args, shell=False, capture_output=True, text=True, encoding="cp866", errors="replace")
	out = (p.stdout or "") + (p.stderr or "")
	return p.returncode, out


def _rasdial_phonebook_path(entry_name: str = VPN_NAME) -> str:
	"""Return PBK path we actually edit/use for rasdial (Unicode-safe)."""
	return find_pbk_path(entry_name=entry_name)


def _windows_short_path(path: str) -> str:
	"""Best-effort convert to 8.3 short path.

	`rasdial` often fails to parse /PHONEBOOK paths containing Cyrillic characters.
	Passing a short (ASCII) path usually fixes it.
	"""
	try:
		import ctypes
		from ctypes import wintypes

		GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
		GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
		GetShortPathNameW.restype = wintypes.DWORD

		# First call to get required size
		n = GetShortPathNameW(path, None, 0)
		if not n:
			return path
		buf = ctypes.create_unicode_buffer(n)
		m = GetShortPathNameW(path, buf, n)
		if not m:
			return path
		return buf.value or path
	except Exception:
		return path


def _rasdial_phonebook_arg(entry_name: str = VPN_NAME) -> str:
	"""Return /PHONEBOOK:<path> arg (prefer 8.3 short path)."""
	pbk = _rasdial_phonebook_path(entry_name=entry_name)
	pbk_eff = _windows_short_path(pbk)
	# Debug: show which PBK file is being used (helps diagnose AllUser vs user mismatch)
	print(f"[VPN] Using PBK: {pbk} (effective: {pbk_eff})")
	return f"/PHONEBOOK:{pbk_eff}"


def _rasdial_connect_args(entry_name: str, username: str, password: str) -> list[str]:
	"""Build rasdial args for connecting.

	Important: for best compatibility place /PHONEBOOK at the *end*.
	Some Windows builds print "USAGE" if /PHONEBOOK is placed before credentials.
	"""
	args = ["rasdial", entry_name]
	if username:
		args.append(username)
		if password:
			args.append(password)
	# switches at the end
	try:
		args.append(_rasdial_phonebook_arg(entry_name=entry_name))
	except Exception:
		pass
	return args


def _rasdial_disconnect_args(entry_name: str) -> list[str]:
	# For disconnect, rasdial help doesn't list /PHONEBOOK; keep it simple.
	return ["rasdial", entry_name, "/disconnect"]


def ensure_disconnected(entry_name: str, wait_sec: float = 6.0) -> None:
	# 1) Пробуем по имени, затем глобально
	cmds = [
		f'rasdial "{entry_name}" /disconnect',
		'rasdial /disconnect',
		# 2) PowerShell: отключить любые активные подключения (системные и пользовательские)
		'powershell -NoProfile -Command "try { Get-VpnConnection -AllUserConnection | ? {$_.ConnectionStatus -ne \'Disconnected\'} | Disconnect-VpnConnection -Force -PassThru | Out-Null } catch {}"',
		'powershell -NoProfile -Command "try { Get-VpnConnection | ? {$_.ConnectionStatus -ne \'Disconnected\'} | Disconnect-VpnConnection -Force -PassThru | Out-Null } catch {}"',
	]
	for _ in range(2):
		for c in cmds:
			run(c)
		# 3) Ждём фактического рассоединения
		t0 = time.time()
		while time.time() - t0 < wait_sec:
			_, out = run_capture('rasdial')
			low = (out or '').lower()
			if 'no connections' in low or 'нет активных подключений' in low:
				run('ipconfig /flushdns')
				return
			if entry_name not in out:
				run('ipconfig /flushdns')
				return
			time.sleep(0.5)
	# best-effort финал
	run('ipconfig /flushdns')

def pbk_paths():
	# Prefer ProgramData (All Users) PBK first. In practice, many VPN entries created by GUI
	# live there and `rasdial` without /PHONEBOOK tends to use it.
	return [
		r"C:\\ProgramData\\Microsoft\\Network\\Connections\\Pbk\\rasphone.pbk",
		os.path.expandvars(r"%AppData%\\Microsoft\\Network\\Connections\\Pbk\\rasphone.pbk"),
	]

def find_pbk_path(entry_name: Optional[str] = None) -> str:
	"""Pick PBK path.

	If entry_name is provided, prefer a PBK that actually contains [entry_name].
	This avoids a common pitfall: having both AllUser and User PBKs, where we edit one file
	but `rasdial` uses the other.
	"""
	candidates = [p for p in pbk_paths() if os.path.exists(p)]
	if not candidates:
		raise FileNotFoundError("rasphone.pbk not found in user or system path")

	if not entry_name:
		return candidates[0]

	# Prefer PBK that contains the entry section
	for p in candidates:
		try:
			raw = read_text_any(p)
			if re.search(rf"(?m)^\[{re.escape(entry_name)}\]$", raw):
				return p
		except Exception:
			continue

	# Fallback: first existing PBK
	return candidates[0]

def read_text_any(path: str) -> str:
	for enc in ("utf-8", "cp1251", "cp1252"):
		try:
			with open(path, "r", encoding=enc) as f:
				return f.read()
		except Exception:
			continue
	with open(path, "r", errors="ignore") as f:
		return f.read()

def write_text_keep_encoding(path: str, content: str):
	for enc in ("utf-8", "cp1251", "cp1252"):
		try:
			with open(path, "r", encoding=enc) as f:
				f.read()
			with open(path, "w", encoding=enc) as f:
				f.write(content)
			return
		except Exception:
			continue
	with open(path, "w", encoding="utf-8") as f:
		f.write(content)

def _ensure_pbk_kv(section_body: str, key: str, value: str) -> str:
	pat = re.compile(rf"(?m)^{re.escape(key)}=.*$")
	if pat.search(section_body):
		return pat.sub(f"{key}={value}", section_body)
	# вставим в начало секции
	return f"{key}={value}\r\n" + section_body


def set_vpn_server_in_pbk(entry_name: str, server: str) -> None:
	path = find_pbk_path(entry_name=entry_name)
	raw = read_text_any(path)

	pat_section = re.compile(rf"(?ms)^\[{re.escape(entry_name)}\]\s*(.*?)(?=^\[|\Z)")
	m = pat_section.search(raw)
	if not m:
		raise RuntimeError(f"Entry [{entry_name}] not found in {path}")

	section_body = m.group(1)

	if re.search(r"(?m)^PhoneNumber=", section_body):
		new_section = re.sub(r"(?m)^PhoneNumber=.*$", f"PhoneNumber={server}", section_body)
	else:
		new_section = f"PhoneNumber={server}\r\n{section_body}"

	new_raw = raw[:m.start(1)] + new_section + raw[m.end(1):]

	try:
		backup = path + ".bak"
		if not os.path.exists(backup):
			with open(backup, "wb") as bf:
				bf.write(raw.encode("utf-8", errors="ignore"))
	except Exception:
		pass

	write_text_keep_encoding(path, new_raw)

def disconnect(entry_name: str) -> None:
	run(f'rasdial "{entry_name}" /disconnect')

def _looks_dns_or_host_error(text: str) -> bool:
	t = text.lower()
	return (
		"этот хост неизвестен" in t or
		"no such host is known" in t or
		"host not known" in t or
		"не удается разрешить имя" in t or
		"could not resolve" in t
	)

def is_connected(entry_name: str) -> bool:
	"""Quiet best-effort check via `rasdial` output.

	We intentionally avoid PowerShell here because it spams "Disconnected" into console
	and is not required for this project's workflow.
	"""
	try:
		_, out = _run_capture_args_silent(["rasdial"])
	except Exception:
		return False
	low = (out or '').lower()
	if (
		'no connections' in low
		or 'нет активных подключений' in low
		or 'отсутствуют подключения' in low
		or 'подключения отсутствуют' in low
	):
		return False
	# If rasdial doesn't explicitly say there are no connections, assume connected.
	return True


def ensure_full_tunnel(entry_name: str) -> None:
	"""Best-effort: force VPN to route all traffic (disable split tunneling).

	Если включён SplitTunneling, rasdial может писать "Connected", но внешний IP не меняется,
	потому что default route остаётся на обычном интерфейсе.

	Важно: запускаем PowerShell БЕЗ shell=True, иначе кавычки/скобки могут ломать try/catch.
	Мы НЕ падаем с ошибкой, только логируем, чтобы не ломать рабочие окружения.
	"""
	# Try both scopes: AllUserConnection and current user.
	ps_script = (
		"& { "
		"$name='" + entry_name + "'; "
		"try { Set-VpnConnection -AllUserConnection -Name $name -SplitTunneling $False -Force -ErrorAction Stop; "
		"Write-Output 'Set-VpnConnection(AllUser) SplitTunneling=False OK' } catch { "
		"Write-Output ('Set-VpnConnection(AllUser) failed: ' + $_.Exception.Message) }; "
		"try { Set-VpnConnection -Name $name -SplitTunneling $False -Force -ErrorAction Stop; "
		"Write-Output 'Set-VpnConnection(User) SplitTunneling=False OK' } catch { "
		"Write-Output ('Set-VpnConnection(User) failed: ' + $_.Exception.Message) } "
		"}"
	)
	try:
		p = subprocess.run(
			[
				"powershell",
				"-NoProfile",
				"-ExecutionPolicy",
				"Bypass",
				"-Command",
				ps_script,
			],
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
		)
		out = (p.stdout or "") + (p.stderr or "")
		if out.strip():
			print(out)
	except Exception as e:
		print(f"Set-VpnConnection call failed: {e}")


def _rasdial_active_text() -> str:
	try:
		_, out = _run_capture_args_silent(["rasdial"])
		return out or ''
	except Exception:
		return ''


def _get_ras_connection_text() -> str:
	"""Best-effort PowerShell check: Get-RasConnection (if available)."""
	try:
		p = subprocess.run(
			[
				"powershell",
				"-NoProfile",
				"-ExecutionPolicy",
				"Bypass",
				"-Command",
				"Get-Command Get-RasConnection -ErrorAction SilentlyContinue | Out-Null; "
				"if ($?) { Get-RasConnection -ErrorAction SilentlyContinue | Format-Table -AutoSize | Out-String }",
			],
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
		)
		return (p.stdout or "").strip()
	except Exception:
		return ""


def connect(entry_name: str, username: str, password: str) -> Tuple[bool, str]:
	rc, out = run_capture(f'rasdial "{entry_name}" {username} {password}')
	if rc != 0:
		return False, out
	return True, out

def external_ip(timeout: int = 20) -> str:
	return requests.get("https://api.ipify.org", timeout=timeout).text.strip()

def host_for_index(idx: int) -> str:
	return f"{HOST_PREFIX}{idx}{HOST_SUFFIX}"

def rotate_and_connect(start: int = 1, end: int = 250, pause_after_connect: float = 1.0, hold: bool = False, only_index: Optional[int] = None, retries: int = 2, backoff: float = 1.5):
	indices = [only_index] if only_index is not None else list(range(start, end + 1))
	for i in indices:
		host = host_for_index(i)
		print(f"\n=== Switching VPN '{VPN_NAME}' to server: {host} ===")

		ok = False
		err_txt = ""
		for attempt in range(1, retries + 1):
			disconnect(VPN_NAME)
			set_vpn_server_in_pbk(VPN_NAME, host)
			ok, err_txt = connect(VPN_NAME, USERNAME, PASSWORD)
			if ok:
				break

			# Если явная DNS/хост-ошибка — нет смысла ретраить долго
			if _looks_dns_or_host_error(err_txt):
				print("Skip: host/DNS error, moving to next server.")
				break

			sleep_s = max(1.0, backoff * attempt)
			print(f"Connect failed (attempt {attempt}/{retries}). Retrying after {sleep_s:.1f}s ...")
			time.sleep(sleep_s)

		if not ok:
			print("Failed to connect to", host, "- skipping.")
			continue

		time.sleep(pause_after_connect)

		try:
			ip = external_ip(timeout=15)
			print("External IP:", ip)
		except Exception as e:
			print("Failed to get external IP:", e)

		if hold:
			input("Connected. Press Enter to switch to the next server...")
		else:
			time.sleep(1)

if __name__ == "__main__":
	import argparse
	ap = argparse.ArgumentParser("VPN IP rotation via rasphone.pbk edit")
	ap.add_argument("--start", type=int, default=1, help="start index (default 1)")
	ap.add_argument("--end", type=int, default=250, help="end index (default 250)")
	ap.add_argument("--index", type=int, default=None, help="connect only to this index (overrides start/end)")
	ap.add_argument("--pause", type=float, default=1.0, help="pause after connect, seconds")
	ap.add_argument("--hold", action="store_true", help="wait for Enter after each connect")
	ap.add_argument("--retries", type=int, default=2, help="retries per server on generic errors")
	ap.add_argument("--backoff", type=float, default=1.5, help="retry backoff base seconds")

	args = ap.parse_args()

	if args.index is not None and (args.index < 1 or args.index > 250):
		raise SystemExit("--index must be in [1..250]")

	rotate_and_connect(
		start=args.start,
		end=args.end,
		pause_after_connect=args.pause,
		hold=args.hold,
		only_index=args.index,
		retries=args.retries,
		backoff=args.backoff
	)