import subprocess, time, requests, os, sys, re
from typing import Optional, Tuple, List

# Параметры вашего VPN-подключения (взято из proxy_v_generator.py)
VPN_NAME = "MySSTP2"
USERNAME = "MikeSD2"
PASSWORD = "07031998Mike"

HOST_SUFFIX = ".hideservers.net"

# Список префиксов (как вы дали), индексы будут подставлены 1..254
PREFIXES = [
	"176-10-106-",
	"41-77-117-",
]

def flush_dns() -> None:
	try:
		run("ipconfig /flushdns")
	except Exception:
		pass
	try:
		run('powershell -NoProfile -Command "Clear-DnsClientCache"')
	except Exception:
		pass

def dns_warmup(host: str, tries: int = 2, wait: float = 0.3) -> None:
	import socket
	for _ in range(tries):
		try:
			socket.getaddrinfo(host, 443)
		except Exception:
			pass
		run(f'ping -n 1 -w 1000 {host}')
		time.sleep(wait)

def run(cmd):
	p = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="cp866", errors="replace")
	out = (p.stdout or "") + (p.stderr or "")
	print(out, end="")
	return p.returncode

def run_capture(cmd) -> Tuple[int, str]:
	p = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="cp866", errors="replace")
	out = (p.stdout or "") + (p.stderr or "")
	print(out, end="")
	return p.returncode, out

def pbk_paths():
	return [
		os.path.expandvars(r"%AppData%\Microsoft\Network\Connections\Pbk\rasphone.pbk"),
		r"C:\ProgramData\Microsoft\Network\Connections\Pbk\rasphone.pbk",
	]

def find_pbk_path() -> str:
	for p in pbk_paths():
		if os.path.exists(p):
			return p
	raise FileNotFoundError("rasphone.pbk not found in user or system path")

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

def set_vpn_server_in_pbk(entry_name: str, server: str) -> None:
	path = find_pbk_path()
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
	t = (text or "").lower()
	return (
		"этот хост неизвестен" in t or
		"no such host is known" in t or
		"host not known" in t or
		"не удается разрешить имя" in t or
		"could not resolve" in t
	)

def connect(entry_name: str, username: str, password: str) -> Tuple[bool, str]:
	rc, out = run_capture(f'rasdial "{entry_name}" {username} {password}')
	if rc != 0:
		return False, out
	return True, out

def external_ip(timeout: int = 15) -> str:
	return requests.get("https://api.ipify.org", timeout=timeout).text.strip()

def scan_all(prefixes: List[str], start: int, end: int, out_file: str, retries: int = 2, backoff: float = 1.5, pause_after_connect: float = 0.8, pause_after_disconnect: float = 0.6, resolve_ip: bool = True, always_flush: bool = False):
	# создаём/очищаем файл результатов один раз
	if out_file:
		with open(out_file, "w", encoding="utf-8") as f:
			f.write("ok_host\texternal_ip\n")

	for pref in prefixes:
		for idx in range(start, end + 1):
			host = f"{pref}{idx}{HOST_SUFFIX}"
			print(f"\n=== Переключаю '{VPN_NAME}' на: {host} ===")

			ok = False
			err_txt = ""
			for attempt in range(1, retries + 1):
				disconnect(VPN_NAME)
				time.sleep(pause_after_disconnect)

				if always_flush:
					flush_dns()

				set_vpn_server_in_pbk(VPN_NAME, host)
				dns_warmup(host)

				ok, err_txt = connect(VPN_NAME, USERNAME, PASSWORD)
				if ok:
					break

				if _looks_dns_or_host_error(err_txt):
					print("DNS/host ошибка — очищаю DNS, тёплый резолвинг и повтор.")
					flush_dns()
					dns_warmup(host)
					# даём ещё одну попытку после чистки
					continue

				sleep_s = max(1.0, backoff * attempt)
				print(f"Проблема подключения (попытка {attempt}/{retries}). Повтор через {sleep_s:.1f}с ...")
				time.sleep(sleep_s)

			if not ok:
				print("Не удалось подключиться:", host)
				continue

			# Небольшая пауза для установления соединения
			time.sleep(pause_after_connect)

			ip_txt = ""
			if resolve_ip:
				try:
					ip_txt = external_ip(timeout=15)
					print("Внешний IP:", ip_txt)
				except Exception as e:
					print("Не удалось получить внешний IP:", e)

			if out_file:
				with open(out_file, "a", encoding="utf-8") as f:
					f.write(f"{host}\t{ip_txt}\n")

			# краткая пауза перед следующей итерацией
			time.sleep(0.5)

if __name__ == "__main__":
	import argparse
	ap = argparse.ArgumentParser("Сканер VPN-хостов: перебирает PREFIX + [1..254] + .hideservers.net и сохраняет успешные")
	ap.add_argument("--start", type=int, default=1, help="начальный индекс (по умолчанию 1)")
	ap.add_argument("--end", type=int, default=254, help="конечный индекс (по умолчанию 254)")
	ap.add_argument("--out", type=str, default="working_vpn_hosts.txt", help="файл для сохранения успешных хостов")
	ap.add_argument("--retries", type=int, default=2, help="число повторов при общих ошибках")
	ap.add_argument("--backoff", type=float, default=1.5, help="база для задержки между ретраями")
	ap.add_argument("--no-ip", action="store_true", help="не запрашивать внешний IP после подключения")
	ap.add_argument("--pause-disconnect", type=float, default=0.6, help="пауза после разрыва VPN перед следующей попыткой")
	ap.add_argument("--always-flush", action="store_true", help="очищать DNS перед каждым подключением (по умолчанию выкл)")
	args = ap.parse_args()

	if args.start < 1 or args.end > 254 or args.start > args.end:
		raise SystemExit("--start/--end должны быть в диапазоне [1..254], и start <= end")

	scan_all(
		prefixes=PREFIXES,
		start=args.start,
		end=args.end,
		out_file=args.out,
		retries=args.retries,
		backoff=args.backoff,
		pause_after_connect=0.8,
		pause_after_disconnect=args.pause_disconnect,
		resolve_ip=not args.no_ip,
		always_flush=args.always_flush
	)