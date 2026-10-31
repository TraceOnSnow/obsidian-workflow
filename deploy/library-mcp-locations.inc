# Include inside the existing example.com HTTPS server block.
location = /library-mcp { include /etc/nginx/conf.d/library-mcp-proxy.inc; }
location ^~ /library-auth/ { include /etc/nginx/conf.d/library-mcp-proxy.inc; }
location = /.well-known/oauth-protected-resource/library-mcp {
    include /etc/nginx/conf.d/library-mcp-proxy.inc;
}
location = /.well-known/oauth-authorization-server/library-auth {
    include /etc/nginx/conf.d/library-mcp-proxy.inc;
}
