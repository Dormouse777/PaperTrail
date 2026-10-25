"""Narrow anonymous Springer OA cookie-init redirects over the pinned transport.

This never reads a browser profile or institution session. Only the public
response_type=cookie initialization and exact selected PDF return are allowed.
"""
from http.cookiejar import CookieJar
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request
from publishers.base import UrllibTransport, reserve_daily_request


class SpringerPublicPDFTransport:
    def __init__(self, pdf_url, governor, budget_directory, *, delegate=None, observer=None):
        parsed = urlsplit(pdf_url)
        if (parsed.scheme != 'https' or parsed.hostname not in {'link.springer.com','link.springernature.com'}
                or not parsed.path.startswith('/content/pdf/') or not parsed.path.endswith('.pdf')
                or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port):
            raise ValueError('PUBLIC_OA_PDF_SCOPE_INVALID')
        self.pdf_url=pdf_url;self.governor=governor;self.directory=budget_directory
        self.delegate=delegate or UrllibTransport();self.observer=observer
        self.consumed=False

    def _redirect(self,current,target,hop):
        parsed=urlsplit(target)
        if (parsed.scheme!='https' or parsed.username or parsed.password
                or parsed.port or parsed.fragment or any(ord(c)<=32 for c in target)):
            raise ValueError('PUBLIC_OA_REDIRECT_REJECTED')
        if current==self.pdf_url and hop==0 and parsed.hostname=='idp.springer.com' and parsed.path=='/authorize':
            query=parse_qs(parsed.query,keep_blank_values=True,strict_parsing=True)
            if (set(query)=={'response_type','client_id','redirect_uri'}
                    and query['response_type']==['cookie']
                    and query['client_id']==['springerlink']
                    and query['redirect_uri']==[self.pdf_url]):
                return target
        if urlsplit(current).hostname=='idp.springer.com' and hop==1 and target==self.pdf_url:
            return target
        raise ValueError('PUBLIC_OA_REDIRECT_REQUIRES_REVIEW')

    def get(self,url,*,headers,timeout,allow_redirects=False):
        if self.consumed or url!=self.pdf_url or headers!={'Accept':'application/pdf'} or allow_redirects is not False:
            raise ValueError('PUBLIC_OA_SINGLE_DISPATCH_REQUIRED')
        self.consumed=True;jar=CookieJar();current=url
        try:
            for hop in range(3):
                if hop:
                    control='https://'+urlsplit(current).hostname+'/'
                    reserve_daily_request(self.directory,control,100)
                    self.governor.acquire(control,download_attempt=False)
                request=Request(current,headers=headers)
                jar.add_cookie_header(request)
                event={'hop':hop,'host':urlsplit(current).hostname,'path':urlsplit(current).path,'status':'intent_before_dispatch'}
                if self.observer:self.observer(dict(event))
                response=self.delegate.get(current,headers=dict(request.header_items()),timeout=timeout,allow_redirects=False)
                event.update(status='received',http_status=response.status)
                if self.observer:self.observer(dict(event))
                if response.status==200 and current==self.pdf_url:return response
                try:
                    if response.status in {401,403,406,429} or response.status>=500:
                        self.governor.record_http_error('https://'+urlsplit(current).hostname+'/',response.status,response.headers.get('Retry-After'))
                        raise ValueError('PUBLIC_OA_ACCESS_REFUSED_NO_RETRY')
                    if response.status not in {301,302,303,307,308}:
                        raise ValueError('PUBLIC_OA_INITIALIZATION_NOT_COMPLETED')
                    target=self._redirect(current,response.headers.get('Location',''),hop)
                    # Standard cookie handling, only from this new in-memory
                    # anonymous exchange. Never serialize cookie/header values.
                    jar.extract_cookies(SimpleNamespace(info=lambda:response.headers),request)
                    current=target
                finally:response.close()
            raise ValueError('PUBLIC_OA_REDIRECT_LIMIT')
        finally:jar.clear()
