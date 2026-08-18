def icerik_var_mi(icerik_tamamlanan):
    """Kullanıcının sorumluluğu için 1/4 ve üzeri içerik mevcut kabul edilir."""
    return icerik_tamamlanan >= 1


def karar_ver(icerik_var, gorsel_var):
    """Bir otelde hangi işçinin çalışacağını belirler."""
    if icerik_var and gorsel_var:
        return "ATLA"
    if icerik_var and not gorsel_var:
        return "SADECE_GORSEL"
    if not icerik_var and gorsel_var:
        return "SADECE_ICERIK"
    return "ICERIK_VE_GORSEL"
