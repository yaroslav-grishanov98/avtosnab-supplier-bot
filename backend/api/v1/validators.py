def mask_inn(inn):
    if not inn:
        return ""
    if len(inn) >= 6:
        return inn[:4] + "*" * (len(inn) - 6) + inn[-2:]
    return inn


def validate_inn(inn):
    digits = inn.replace(" ", "")
    if not digits.isdigit():
        return "ИНН должен содержать только цифры."
    if len(digits) == 10:
        w = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        check = sum(w[i] * int(digits[i]) for i in range(9)) % 11 % 10
        if check != int(digits[9]):
            return "ИНН не прошёл проверку контрольной суммы — проверьте правильность ввода."
        return None
    if len(digits) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c1 = sum(w1[i] * int(digits[i]) for i in range(10)) % 11 % 10
        c2 = sum(w2[i] * int(digits[i]) for i in range(11)) % 11 % 10
        if c1 != int(digits[10]) or c2 != int(digits[11]):
            return "ИНН не прошёл проверку контрольной суммы — проверьте правильность ввода."
        return None
    return "ИНН должен содержать 10 цифр (для компаний) или 12 цифр (для ИП)."
