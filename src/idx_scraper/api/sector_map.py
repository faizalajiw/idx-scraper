"""Ticker -> sector mapping (IDX-IC style, curated for liquid names).

Maintained by hand: the free IDX endpoints polled by this project do not
include the official IDX-IC sector classification. Stocks not present in this
map fall into "Lainnya" in the sector analysis.
"""

SECTOR_MAP: dict[str, str] = {
    # ---- Perbankan & Keuangan ----
    "BBCA": "Perbankan", "BBRI": "Perbankan", "BMRI": "Perbankan", "BBNI": "Perbankan",
    "BRIS": "Perbankan", "BBTN": "Perbankan", "BNGA": "Perbankan", "BRIN": "Perbankan",
    "NISP": "Perbankan", "PNBN": "Perbankan", "MEGA": "Perbankan", "ARTO": "Perbankan",
    "BBKP": "Keuangan Non-Bank", "BFIN": "Keuangan Non-Bank", "CRED": "Keuangan Non-Bank",
    "BTPS": "Perbankan", "AGRO": "Perbankan", "BJBR": "Perbankan", "BKSL": "Keuangan Non-Bank",
    "CYBR": "Keuangan Non-Bank", "NOBU": "Perbankan", "BVIC": "Keuangan Non-Bank",
    "PNLF": "Keuangan Non-Bank", "PFIN": "Keuangan Non-Bank", "MFIN": "Keuangan Non-Bank",
    "AHAP": "Asuransi", "LIFE": "Asuransi", "ASRM": "Asuransi", "AMAG": "Asuransi",
    "PRDA": "Asuransi", "HEAL": "Asuransi", "AHPC": "Asuransi",
    # ---- Energi & Batubara ----
    "PGAS": "Energi", "MEDC": "Energi", "ADRO": "Batubara", "ITMG": "Batubara",
    "PTBA": "Batubara", "BYAN": "Batubara", "INDY": "Batubara", "HRUM": "Batubara",
    "PTRO": "Batubara", "BSSR": "Batubara", "DEWA": "Energi", "ENRG": "Energi",
    "COAL": "Batubara", "KKGI": "Batubara", "GEMS": "Batubara", "SMMT": "Batubara",
    "DSSA": "Energi", "RUIS": "Energi", "RAJA": "Energi",
    # ---- Logam & Mineral ----
    "ANTM": "Logam & Mineral", "INCO": "Logam & Mineral", "MDKA": "Logam & Mineral",
    "NCKL": "Logam & Mineral", "TINS": "Logam & Mineral", "PSAB": "Logam & Mineral",
    "BRMS": "Logam & Mineral", "ARCI": "Logam & Mineral", "DKFT": "Logam & Mineral",
    "ZINC": "Logam & Mineral", "NICL": "Logam & Mineral", "TMSO": "Logam & Mineral",
    "MBMA": "Logam & Mineral", "CNKO": "Logam & Mineral", "GOLD": "Logam & Mineral",
    "EMAS": "Logam & Mineral", "AMMN": "Logam & Mineral", "MRAT": "Logam & Mineral",
    # ---- Properti & Real Estat ----
    "BSDE": "Properti", "SMRA": "Properti", "PWON": "Properti", "CTRA": "Properti",
    "ASRI": "Properti", "DMAS": "Properti", "PANI": "Properti", "ARNA": "Properti",
    "BEST": "Properti", "DPUM": "Properti", "GPRA": "Properti", "KOTA": "Properti",
    "LPKR": "Properti", "MDLN": "Properti", "NIKL": "Properti", "RSCH": "Properti",
    "APLN": "Properti", "DILD": "Properti", "JRPT": "Properti", "OMRE": "Properti",
    "IMPC": "Properti", "BIPP": "Properti", "BUVA": "Properti", "CBRE": "Properti",
    # ---- Telkom & Internet ----
    "TLKM": "Telkom & Internet", "ISAT": "Telkom & Internet", "EXCL": "Telkom & Internet",
    "BUKA": "Telkom & Internet", "EMTK": "Telkom & Internet", "MTDL": "Telkom & Internet",
    "OASA": "Telkom & Internet", "TFAS": "Telkom & Internet", "WIFI": "Telkom & Internet",
    "INET": "Telkom & Internet", "SPACE": "Telkom & Internet",
    # ---- Infrastruktur & Konstruksi ----
    "TOWR": "Infrastruktur", "MTEL": "Infrastruktur", "TBIG": "Infrastruktur",
    "BALI": "Infrastruktur", "LINK": "Infrastruktur", "JSMR": "Infrastruktur",
    "WIKA": "Konstruksi", "PTPP": "Konstruksi", "ADHI": "Konstruksi", "WSBP": "Konstruksi",
    "WSKT": "Konstruksi", "ACES": "Infrastruktur", "SHIP": "Infrastruktur",
    "JECC": "Infrastruktur", "CMNP": "Infrastruktur", "IPCM": "Infrastruktur",
    "YULE": "Konstruksi",
    # ---- Transportasi & Logistik ----
    "UNTR": "Transportasi & Logistik", "GIAA": "Transportasi & Logistik",
    "SMDR": "Transportasi & Logistik", "TMAS": "Transportasi & Logistik",
    "AKRA": "Transportasi & Logistik", "PSSI": "Transportasi & Logistik",
    "HAIS": "Transportasi & Logistik", "BIRD": "Transportasi & Logistik",
    "SAPX": "Transportasi & Logistik", "ELPI": "Transportasi & Logistik",
    "RIGS": "Transportasi & Logistik", "BULL": "Transportasi & Logistik",
    "KALI": "Transportasi & Logistik", "BPTR": "Transportasi & Logistik",
    "ASSA": "Transportasi & Logistik", "SDMU": "Transportasi & Logistik",
    # ---- Konsumer Primer & Agriculture ----
    "AMRT": "Konsumer Primer", "ICBP": "Konsumer Primer", "INDF": "Konsumer Primer",
    "CPIN": "Konsumer Primer", "JPFA": "Konsumer Primer", "AALI": "Agriculture",
    "LSIP": "Agriculture", "SSMS": "Agriculture", "TAPG": "Konsumer Primer",
    "STAA": "Konsumer Primer", "BWPT": "Agriculture", "DSNG": "Agriculture",
    "SIMP": "Konsumer Primer", "GGRM": "Konsumer Primer", "HMSP": "Konsumer Primer",
    "WIIM": "Konsumer Primer", "RMKE": "Agriculture", "TBLA": "Agriculture",
    "SMAR": "Agriculture", "UNVR": "Konsumer Primer", "MYOR": "Konsumer Primer",
    "SKLT": "Konsumer Primer", "CAMP": "Konsumer Primer", "ROTI": "Konsumer Primer",
    # ---- Kesehatan ----
    "KLBF": "Kesehatan", "KAEF": "Kesehatan", "SIDO": "Kesehatan", "DVLA": "Kesehatan",
    "PYFA": "Kesehatan", "MIKA": "Kesehatan", "SILO": "Kesehatan", "SAME": "Kesehatan",
    # ---- Konsumer Sekunder, Media, Otomotif ----
    "MERK": "Konsumer Sekunder", "SCMA": "Media & Hiburan", "MNCN": "Media & Hiburan",
    "FILM": "Media & Hiburan", "MSIN": "Media & Hiburan", "VIVA": "Media & Hiburan",
    "CMRY": "Media & Hiburan", "BAYU": "Konsumer Sekunder", "AUTO": "Otomotif",
    "ASII": "Otomotif", "IMAS": "Otomotif", "GJTL": "Otomotif", "INDS": "Otomotif",
    "BOLT": "Otomotif",
    # ---- Industri Dasar & Kimia ----
    "INKP": "Industri Dasar & Kimia", "TKIM": "Industri Dasar & Kimia",
    "BRPT": "Industri Dasar & Kimia", "SMGR": "Industri Dasar & Kimia",
    "INTP": "Industri Dasar & Kimia", "UNSP": "Industri Dasar & Kimia",
    "ESSA": "Industri Dasar & Kimia", "AGII": "Industri Dasar & Kimia",
    "BAJA": "Industri Dasar & Kimia", "KRAS": "Industri Dasar & Kimia",
    "SRSN": "Industri Dasar & Kimia", "TPIA": "Industri Dasar & Kimia",
    "AGIJ": "Industri Dasar & Kimia", "MDIA": "Industri Dasar & Kimia",
    "EAST": "Industri Dasar & Kimia", "KIJA": "Industri Dasar & Kimia",
    # ---- Utilitas & Energi Terbarukan ----
    "PGEO": "Utilitas", "KEEN": "Utilitas", "POWR": "Utilitas", "CITA": "Utilitas",
    "BREN": "Utilitas", "ATPK": "Utilitas", "GLVA": "Utilitas",
    # ---- Teknologi ----
    "COIN": "Teknologi", "DMMX": "Teknologi", "AXIO": "Teknologi", "IOTF": "Teknologi",
    # ---- Retail ----
    "MAPA": "Retail", "RALS": "Retail", "LPPF": "Retail", "ERAJ": "Retail",
    "DOOH": "Retail", "MPMX": "Retail", "CSAP": "Retail", "WAPO": "Retail",
    "WINS": "Retail",
}
