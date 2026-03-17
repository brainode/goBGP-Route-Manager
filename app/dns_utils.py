import dns.resolver

def resolve_domain(domain):
    ip_list = []
    try:
        answers = dns.resolver.resolve(domain, 'A')
        for rdata in answers:
            ip_list.append(rdata.address)
    except Exception as e:
        print(f"Ошибка резолва DNS: {e}")
    return ip_list