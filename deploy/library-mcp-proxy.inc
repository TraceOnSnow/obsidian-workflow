# Included only for the explicitly reserved private-library routes.
# Disable access logs: OAuth queries can contain temporary credentials/state.
access_log off;
proxy_pass http://unix:/etc/nginx/conf.d/library-mcp/mcp.sock;
proxy_http_version 1.1;
proxy_set_header Host example.com;
proxy_set_header X-Forwarded-Host example.com;
proxy_set_header X-Forwarded-Proto https;
proxy_set_header Connection "";
proxy_buffering off;
proxy_request_buffering off;
proxy_read_timeout 120s;
proxy_send_timeout 120s;
client_max_body_size 1m;
add_header Cache-Control "no-store" always;
add_header X-Content-Type-Options nosniff always;
# Preserve Origin on same-origin form POSTs; omit cross-origin Referer.
add_header Referrer-Policy same-origin always;
add_header X-Frame-Options DENY always;
