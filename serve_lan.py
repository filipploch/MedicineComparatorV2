#!/usr/bin/env python3
"""Uruchamia aplikację w sieci lokalnej (WiFi) przez HTTPS, do testów na telefonie.

Przeglądarki dają dostęp do aparatu tylko stronom HTTPS (albo localhost), dlatego skrypt tworzy
certyfikat z adresem IP tego komputera w sieci WiFi. Telefon za pierwszym razem pokaże ostrzeżenie
o niezaufanym certyfikacie: trzeba je zaakceptować ("Zaawansowane" -> "Przejdź do strony").

    python serve_lan.py                # https://<IP-komputera>:8443
    python serve_lan.py --port 9000
    python serve_lan.py --http         # bez HTTPS (aparat w telefonie nie zadziała)

To serwer do testów, nie do publicznego wdrożenia (tam: gunicorn + nginx, opis w README).
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import socket
from pathlib import Path

from app import BASE_DIR, create_app

CERT_DIR = BASE_DIR / "instance"


def lan_ip() -> str:
    """Adres IP komputera w sieci lokalnej (bez wysyłania czegokolwiek: UDP connect tylko wybiera interfejs)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def ensure_certificate(ip: str) -> tuple[Path, Path]:
    """Tworzy (albo odświeża po zmianie adresu IP) certyfikat z podpisem własnym."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_path, key_path = CERT_DIR / "lan-cert.pem", CERT_DIR / "lan-key.pem"
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=dt.timezone.utc)
        if ipaddress.ip_address(ip) in sans.get_values_for_type(x509.IPAddress) and \
                not_after > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7):
            return cert_path, key_path

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Porownywarka (test) {ip}")])
    now = dt.datetime.now(dt.timezone.utc)
    hostname = socket.gethostname()
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address(ip)), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.DNSName("localhost"), x509.DNSName(hostname), x509.DNSName(f"{hostname}.local")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def print_qr(url: str) -> None:
    try:
        import qrcode  # opcjonalnie: pip install qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Serwer testowy w sieci WiFi")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--http", action="store_true", help="bez HTTPS (aparat w telefonie nie zadziała)")
    ap.add_argument("--ip", help="adres IP do certyfikatu, jeśli wykryty jest niewłaściwy")
    args = ap.parse_args()

    app = create_app()
    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if db_uri.startswith("sqlite:///") and not Path(db_uri.removeprefix("sqlite:///")).exists():
        print("Brak bazy. Najpierw: flask --app wsgi init-db (i opcjonalnie load-registry / load-chpl).")
        return

    ip = args.ip or lan_ip()
    ssl_context = None
    if not args.http:
        ssl_context = tuple(str(p) for p in ensure_certificate(ip))
    url = f"{'http' if args.http else 'https'}://{ip}:{args.port}/"
    print(f"\nAdres dla telefonu w tej samej sieci WiFi:  {url}")
    if not args.http:
        print("Certyfikat jest wystawiony przez ten komputer: przy pierwszym wejściu zaakceptuj ostrzeżenie.")
    print_qr(url)
    print("Zatrzymanie: Ctrl+C\n")
    app.run(host="0.0.0.0", port=args.port, ssl_context=ssl_context, threaded=True, debug=False)


if __name__ == "__main__":
    main()
