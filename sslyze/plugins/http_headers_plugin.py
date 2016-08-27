# -*- coding: utf-8 -*-
"""Plugin to test the server for the presence of the HTTP Strict Transport Security and HTTP Public Key Pinning
headers.
"""

import Cookie
from urlparse import urlparse
from xml.etree.ElementTree import Element

from sslyze.plugins import plugin_base
from sslyze.plugins.certificate_info_plugin import CertInfoFullResult, Certificate
from sslyze.plugins.plugin_base import PluginResult
from sslyze.ssl_settings import TlsWrappedProtocolEnum
from sslyze.utils.http_response_parser import parse_http_response


class HttpHeadersPlugin(plugin_base.PluginBase):

    interface = plugin_base.PluginInterface(title="HttpHeadersPlugin", description='')
    interface.add_command(
        command="http_headers",
        help="Checks for the HTTP Strict Transport Security (HSTS) and HTTP Public Key Pinning (HPKP) HTTP headers "
             "within the response sent back by the server(s). Also computes the HPKP pins for the server(s)' current "
             "certificate chain."
    )


    def process_task(self, server_info, command, options_dict=None):

        if server_info.tls_wrapped_protocol not in [TlsWrappedProtocolEnum.PLAIN_TLS, TlsWrappedProtocolEnum.HTTPS]:
            raise ValueError('Cannot test for HTTP headers on a StartTLS connection.')

        hsts_header, hpkp_header, hpkp_report_only, certificate_chain = self._get_hsts_header(server_info)
        return HttpHeadersResult(server_info, command, options_dict, hsts_header, hpkp_header, hpkp_report_only,
                                 certificate_chain)



    MAX_REDIRECT = 5

    def _get_hsts_header(self, server_info):
        certificate_chain = None
        hsts_header = None
        hpkp_header = None
        hpkp_report_only = False
        nb_redirect = 0
        http_get_format = 'GET {0} HTTP/1.0\r\nHost: {1}\r\n{2}Connection: close\r\n\r\n'.format
        http_path = '/'
        http_append = ''

        while nb_redirect < self.MAX_REDIRECT:
            # Always use a new connection as some servers always close the connection after sending back an HTTP
            # response
            ssl_connection = server_info.get_preconfigured_ssl_connection()

            # Perform the SSL handshake
            ssl_connection.connect()
            certificate_chain = ssl_connection.get_peer_cert_chain()

            ssl_connection.write(http_get_format(http_path, server_info.hostname, http_append))
            http_resp = parse_http_response(ssl_connection)
            ssl_connection.close()
            
            if http_resp.version == 9 :
                # HTTP 0.9 => Probably not an HTTP response
                raise ValueError('Server did not return an HTTP response')
            else:
                hsts_header = http_resp.getheader('strict-transport-security', None)
                hpkp_header = http_resp.getheader('public-key-pins', None)
                if hpkp_header is None:
                    hpkp_report_only = True
                    hpkp_header = http_resp.getheader('public-key-pins-report-only', None)

            # If there was no HSTS header, check if the server returned a redirection
            if hsts_header is None and 300 <= http_resp.status < 400:
                redirect_header = http_resp.getheader('Location', None)
                cookie_header = http_resp.getheader('Set-Cookie', None)
                
                if redirect_header is None:
                    break
                o = urlparse(redirect_header)
                
                # Handle absolute redirection URL but only allow redirections to the same domain and port
                if o.hostname and o.hostname != server_info.hostname:
                    break
                else:
                    http_path = o.path
                    if o.scheme == 'http':
                        # We would have to use urllib for http: URLs
                        break

                # Handle cookies
                if cookie_header:
                    cookie = Cookie.SimpleCookie(cookie_header)
                    if cookie:
                        http_append = 'Cookie:' + cookie.output(attrs=[], header='', sep=';') + '\r\n'

                nb_redirect+=1
            else:
                # If the server did not return a redirection just give up
                break

        return hsts_header, hpkp_header, hpkp_report_only, certificate_chain


class ParsedHstsHeader(object):

    def __init__(self, raw_hsts_header):
        self.max_age = None
        self.include_subdomains = False
        self.preload = False

        for hsts_directive in raw_hsts_header.split(';'):
            hsts_directive = hsts_directive.strip()
            if 'max-age' in hsts_directive:
                self.max_age = hsts_directive.split('max-age=')[1].strip()
            elif 'includeSubDomains' in hsts_directive:
                self.include_subdomains = True
            elif 'preload' in hsts_directive:
                self.preload = True
            else:
                raise ValueError('Unexpected value in HSTS header: {}'.format(hsts_directive))


class ParsedHpkpHeader(object):

    def __init__(self, raw_hpkp_header, report_only=False):
        self.report_only = report_only
        self.report_uri = None
        self.include_subdomains = False
        self.max_age = None

        pin_sha256_list = []
        for hpkp_directive in raw_hpkp_header.split(';'):
            hpkp_directive = hpkp_directive.strip()
            if 'pin-sha256' in hpkp_directive:
                pin_sha256_list.append(hpkp_directive.split('pin-sha256=')[1].strip(' "'))
            elif 'max-age' in hpkp_directive:
                self.max_age = hpkp_directive.split('max-age=')[1].strip()
            elif 'includeSubDomains' in hpkp_directive:
                self.include_subdomains = True
            elif 'report-uri' in hpkp_directive:
                self.report_uri = hpkp_directive.split('report-uri=')[1].strip(' "')
            else:
                raise ValueError('Unexpected value in HPKP header: {}'.format(hpkp_directive))

        self.pin_sha256_list = pin_sha256_list


class HttpHeadersResult(PluginResult):
    """The result of running --http_headers on a specific server.

    Attributes:
        hsts_header (ParsedHstsHeader): The content of the HSTS header returned by the server; None if no HSTS header
            was returned.
        hpkp_header (ParsedHpkpHeader): The content of the HPKP header returned by the server; None if no HPKP header
            was returned.
        is_valid_pin_configured (bool): True if at least one of the configured pins was found in the server's
            verified certificate chain. None if the verified chain could not be built or no HPKP header was returned.
        is_backup_pin_configured (bool): True if if at least one of the configured pins was NOT found in the server's
            verified certificate chain. None if the verified chain could not be builtor no HPKP header was returned.
        verified_certificate_chain (List[Certificate]): The verified certificate chain; index 0 is the leaf
            certificate and the last element is the anchor/CA certificate from the Mozilla trust store. Will be empty if
            validation failed or the verified chain could not be built. The HPKP pin for each certificate is available
            in the certificate's hpkp_pin attribute.

    """

    COMMAND_TITLE = 'HTTP Security Headers'

    def __init__(self, server_info, plugin_command, plugin_options, hsts_header, hpkp_header, hpkp_report_only,
                 certificate_chain):
        super(HttpHeadersResult, self).__init__(server_info, plugin_command, plugin_options)
        self.hsts_header = ParsedHstsHeader(hsts_header) if hsts_header else None
        self.hpkp_header = ParsedHpkpHeader(hpkp_header, hpkp_report_only) if hpkp_header else None

        # Hack: use function in CertificateInfoPlugin to get the verified certificate chain so we can check the pins
        self.verified_certificate_chain = CertInfoFullResult._build_verified_certificate_chain(
            [Certificate(x509_cert) for x509_cert in certificate_chain])

        # Is the pinning configuration valid?
        self.is_valid_pin_configured = None
        self.is_backup_pin_configured = None
        if self.verified_certificate_chain and self.hpkp_header:
            # Is one of the configured pins in the current server chain?
            self.is_valid_pin_configured = False
            server_pin_list = [cert.hpkp_pin for cert in self.verified_certificate_chain]
            for pin in self.hpkp_header.pin_sha256_list:
                if pin in server_pin_list:
                    self.is_valid_pin_configured = True
                    break

            # Is a backup pin configured?
            self.is_backup_pin_configured = set(self.hpkp_header.pin_sha256_list) != set(server_pin_list)


    PIN_TXT_FORMAT = '      {0:<80}{1}'.format

    def as_text(self):
        txt_result = [self.PLUGIN_TITLE_FORMAT('HTTP Strict Transport Security (HSTS)')]

        if self.hsts_header:
            txt_result.append(self.FIELD_FORMAT("Max Age:", self.hsts_header.max_age))
            txt_result.append(self.FIELD_FORMAT("Include Subdomains:", self.hsts_header.include_subdomains))
            txt_result.append(self.FIELD_FORMAT("Preload:", self.hsts_header.preload))
        else:
            txt_result.append(self.FIELD_FORMAT("NOT SUPPORTED - Server did not send an HSTS header", ""))

        computed_hpkp_pins_text = ['', self.PLUGIN_TITLE_FORMAT('Computed HPKP Pins for Current Chain')]
        if self.verified_certificate_chain:
            for index, cert in enumerate(self.verified_certificate_chain, start=0):
                cert_subject = CertInfoFullResult._extract_subject_cn_or_oun(cert)
                computed_hpkp_pins_text.append(self.PIN_TXT_FORMAT(('{} - {}'.format(index, cert_subject)),
                                                                   cert.hpkp_pin))
        else:
            computed_hpkp_pins_text.append(self.FIELD_FORMAT("ERROR - Could not build verified chain", ""))

        txt_result.extend(['', self.PLUGIN_TITLE_FORMAT('HTTP Public Key Pinning (HPKP)')])
        if self.hpkp_header:
            txt_result.append(self.FIELD_FORMAT("Max Age:", self.hpkp_header.max_age))
            txt_result.append(self.FIELD_FORMAT("Include Subdomains:", self.hpkp_header.include_subdomains))
            txt_result.append(self.FIELD_FORMAT("Report URI:", self.hpkp_header.report_uri))
            txt_result.append(self.FIELD_FORMAT("Report Only:", self.hpkp_header.report_only))
            txt_result.append(self.FIELD_FORMAT("SHA-256 Pin List:", ', '.join(self.hpkp_header.pin_sha256_list)))

            if self.verified_certificate_chain:
                pin_validation_txt = 'OK - One of the configured pins was found in the certificate chain' \
                    if self.is_valid_pin_configured \
                    else 'FAILED - Could NOT find any of the configured pins in the certificate chain!'
                txt_result.append(self.FIELD_FORMAT("Valid Pin:", pin_validation_txt))

                backup_txt = 'OK - Backup pin found in the configured pins' \
                    if self.is_backup_pin_configured \
                    else 'FAILED - No backup pin found: all the configured pins are in the certificate chain!'
                txt_result.append(self.FIELD_FORMAT("Backup Pin:", backup_txt))

        else:
            txt_result.append(self.FIELD_FORMAT("NOT SUPPORTED - Server did not send an HPKP header", ""))

        # Dispay computed HPKP pins last
        txt_result.extend(computed_hpkp_pins_text)

        return txt_result


    def as_xml(self):
        xml_result = Element(self.plugin_command, title=self.COMMAND_TITLE)

        # HSTS header
        is_hsts_supported = True if self.hsts_header else False
        xml_hsts_attr = {'isSupported': str(is_hsts_supported)}
        if is_hsts_supported:
            xml_hsts_attr['maxAge'] = self.hsts_header.max_age
            xml_hsts_attr['includeSubDomains'] = str(self.hsts_header.include_subdomains)
            xml_hsts_attr['preload'] = str(self.hsts_header.preload)

        xml_hsts = Element('httpStrictTransportSecurity', attrib=xml_hsts_attr)
        xml_result.append(xml_hsts)

        # HPKP header
        is_hpkp_support = True if self.hpkp_header else False
        xml_hpkp_attr = {'isSupported': str(is_hpkp_support)}
        xml_pin_list = []
        if is_hpkp_support:
            xml_hpkp_attr['maxAge'] = self.hpkp_header.max_age
            xml_hpkp_attr['includeSubDomains'] = str(self.hpkp_header.include_subdomains)
            xml_hpkp_attr['reportOnly'] = str(self.hpkp_header.report_only)
            xml_hpkp_attr['reportUri'] = str(self.hpkp_header.report_uri)

            if self.verified_certificate_chain:
                xml_hpkp_attr['isValidPinConfigured'] = str(self.is_valid_pin_configured)
                xml_hpkp_attr['isBackupPinConfigured'] = str(self.is_backup_pin_configured)

            for pin in self.hpkp_header.pin_sha256_list:
                xml_pin = Element('pinSha256')
                xml_pin.text = pin
                xml_pin_list.append(xml_pin)

        xml_hpkp = Element('httpPublicKeyPinning', attrib=xml_hpkp_attr)
        for xml_pin in xml_pin_list:
            xml_hpkp.append(xml_pin)
        xml_result.append(xml_hpkp)

        return xml_result