# Estimated asking prices in the local currency of each market (street/flea-market levels, Oct 2026).
P = {
 # Ban Tawai handicraft village, Chiang Mai (THB)
 'B001':350,'B002':450,'B003':120,'B004':180,'B005':250,'B006':1800,'B007':300,'B008':220,'B009':200,'B010':280,
 'B011':900,'B012':2500,'B013':450,'B014':150,'B015':600,'B016':200,'B017':350,'B018':3500,'B019':1200,'B020':5500,
 'B021':90,'B022':2800,'B023':4500,'B024':750,'B025':650,'B026':400,'B027':3200,'B028':900,'B029':1800,
 # Chelsea Flea, New York (USD)
 'C001':15,'C002':180,'C003':35,'C004':85,'C005':340,'C006':220,'C007':45,'C008':120,'C009':95,'C010':260,
 'C011':110,'C012':55,'C013':40,'C014':65,'C015':650,'C016':90,'C017':240,'C018':180,'C019':60,'C020':45,
 'C021':25,'C022':75,'C023':40,'C024':120,'C025':20,'C026':140,'C027':12,'C028':12,'C029':10,'C030':150,
 # Chiang Mai night market (THB)
 'M001':150,'M002':200,'M003':200,'M004':200,'M005':250,'M006':250,'M007':800,'M008':450,'M009':1200,'M010':1500,
 'M011':1200,'M012':350,'M013':120,'M014':300,'M015':200,'M017':450,'M018':150,'M019':1800,'M020':180,'M022':650,
 # Kerobokan furniture street, Bali (IDR)
 'K001':6500000,'K002':1200000,'K003':1500000,'K005':250000,'K006':7500000,'K007':5500000,'K008':2200000,'K009':3200000,
 'K010':1400000,'K011':4500000,'K012':1800000,'K013':9500000,'K014':1100000,'K015':4500000,'K016':1600000,
}
CUR = {'B': ('THB', 34.0), 'M': ('THB', 34.0), 'C': ('USD', 1.0), 'K': ('IDR', 16300.0)}
import json
d = json.load(open('catalog.json'))
missing = []
for it in d['items']:
    c = it['code']
    if c not in P: missing.append(c); continue
    cur, rate = CUR[c[0]]
    it['price'] = {'amount': P[c], 'currency': cur, 'usd': round(P[c] / rate)}
json.dump(d, open('catalog.json', 'w'), indent=1)
print('priced', sum(1 for i in d['items'] if 'price' in i), 'of', len(d['items']), 'missing', missing)
