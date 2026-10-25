"""Pekerjaan scheduler (dipanggil ``cli._job_*``) yang dipisah dari CLI.

Logika yang tadinya hidup di ``scripts/*.py`` dan diimpor lewat
``from scripts.X import ...`` dipindah ke sini supaya paket ``idx_scraper``
tidak lagi mengutak-atik ``sys.path`` (FAI-20). Berkas ``scripts/*.py``
yang lama tetap ada sebagai pembungkus tipis untuk pemakaian manual.
"""
