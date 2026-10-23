import ipaddress

from cryptography import x509

import serve_lan


def test_certificate_contains_lan_ip_and_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_lan, "CERT_DIR", tmp_path)
    cert_path, _ = serve_lan.ensure_certificate("192.168.1.20")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    ips = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
    assert ipaddress.ip_address("192.168.1.20") in ips
    first = cert_path.read_bytes()
    serve_lan.ensure_certificate("192.168.1.20")
    assert cert_path.read_bytes() == first                 # ten sam adres: certyfikat bez zmian
    serve_lan.ensure_certificate("192.168.1.33")
    assert cert_path.read_bytes() != first                 # nowy adres w sieci: nowy certyfikat
