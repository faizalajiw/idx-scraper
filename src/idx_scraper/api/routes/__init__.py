"""Registrar rute per domain (FAI-6).

Setiap modul mendefinisikan `route = Router()` dengan dekorator ditunda;
`api/app.py` memanggil `route.register(app)` dengan urutan yang sama seperti
saat rute-rute ini masih berada di satu berkas.
"""
