#!/bin/sh
# Certificates for the load balancer's TLS listener, made once by `make obs/up`
# into tls/out (never committed): a CA the TLS exporter trusts, and one server
# certificate per TLS fault (make obs/chaos/tls-*).
#
#   server.pem      s3.storage.internal, valid a year         (the normal case)
#   wrong-name.pem  other.storage.internal                    (name mismatch)
#   expired.pem     s3.storage.internal, ended in 2020        (expired)
#   expiring.pem    s3.storage.internal, ends in 10 days      (the 14-day warning)
#   kube-api.pem    kube-api, valid a year                    (the fake Kubernetes API)
#
# HAProxy wants the certificate and its key in one file.
set -eu
out="${1:-/tls/out}"
mkdir -p "${out}"
cd "${out}"

openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=s3-exporter demo CA" \
	-keyout ca-key.pem -out ca.pem 2>/dev/null

issue() { # name cn validity-options...
	name="$1" cn="$2"
	shift 2
	openssl req -newkey rsa:2048 -nodes -subj "/CN=${cn}" \
		-keyout "${name}.key" -out "${name}.csr" 2>/dev/null
	printf 'subjectAltName=DNS:%s\n' "${cn}" >"${name}.ext"
	openssl x509 -req -in "${name}.csr" -CA ca.pem -CAkey ca-key.pem -CAcreateserial \
		-extfile "${name}.ext" "$@" -out "${name}.crt" 2>/dev/null
	cat "${name}.crt" "${name}.key" >"${name}.pem"
	rm -f "${name}.csr" "${name}.ext" "${name}.crt" "${name}.key"
}

issue server s3.storage.internal -days 365
issue wrong-name other.storage.internal -days 365
issue expired s3.storage.internal -not_before 20200101000000Z -not_after 20200201000000Z
issue expiring s3.storage.internal -days 10
issue kube-api kube-api -days 365

# Read by HAProxy's own user; test material only.
chmod 644 ./*.pem
rm -f ca.srl
echo "TLS material in ${out}: ca.pem server.pem wrong-name.pem expired.pem expiring.pem kube-api.pem"
