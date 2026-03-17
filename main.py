# import socket
# import requests
# import ipinfo
# import pprint

# # from cymru.ip2asn.dns import DNSClient

# # cymru = DNSClient()

# # def domain_to_asns(domain):
# #     ips = {ai[4][0] for ai in socket.getaddrinfo(domain, None)}
# #     return {cymru.lookup(ip, qType="IP").asn for ip in ips}

# def asn_to_prefixes(asn):
#     r = requests.get(f"https://api.bgpview.io/asn/{asn}/prefixes")
#     return [p["prefix"] for p in r.json()["data"]["ipv4_prefixes"]]

# # def get_service_prefixes(domain):
# #     prefixes = set()
# #     for asn in domain_to_asns(domain):
# #         prefixes |= set(asn_to_prefixes(asn))
# #     return prefixes


# # access_token = '80791490280a67'
# # handler = ipinfo.getHandler(access_token)
# # ip_address = '64.233.161.136'
# # details = handler.getDetails(ip_address)
# # pprint.pprint(details.all)
# def main():
#     # asns = domain_to_asns("youtube.com")
#     prefixes = asn_to_prefixes("AS13335")
#     # service_prefixes = get_service_prefixes(prefixes)
#     with open("iplist.txt", "a") as f:
#       for prefix in prefixes:
#          f.write('\''+prefix+'\'\n')
        
    
# # AS13335
# main()