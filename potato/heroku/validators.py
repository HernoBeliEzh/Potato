class ValidationError(ValueError):
    __slots__ = ()


class String:
    def __init__(self, min_len=None, max_len=None, length=None):
        self.min_len = min_len
        self.max_len = max_len
        self.length = length

    def validate(self, value):
        value = str(value)
        if self.min_len is not None and len(value) < self.min_len or self.max_len is not None and len(value) > self.max_len or self.length is not None and len(value) != self.length:
            raise ValidationError("Некорректная длина строки")
        return value


class Integer:
    def __init__(self, minimum=None, maximum=None, digits=None):
        self.minimum = minimum
        self.maximum = maximum
        self.digits = digits

    def validate(self, value):
        try:
            result = int(value)
        except (ValueError, TypeError) as error:
            raise ValidationError("Ожидается целое число") from error
        if self.minimum is not None and result < self.minimum or self.maximum is not None and result > self.maximum or self.digits is not None and len(str(abs(result))) != self.digits:
            raise ValidationError("Число вне допустимого диапазона")
        return result


class Float(Integer):
    def validate(self, value):
        try:
            result = float(value)
        except (ValueError, TypeError) as error:
            raise ValidationError("Ожидается число") from error
        if self.minimum is not None and result < self.minimum or self.maximum is not None and result > self.maximum:
            raise ValidationError("Число вне допустимого диапазона")
        return result


class Boolean:
    def validate(self, value):
        if value in (True, "true", "True", "1", 1):
            return True
        if value in (False, "false", "False", "0", 0):
            return False
        raise ValidationError("Ожидается true или false")


class Choice:
    def __init__(self, possible_values):
        self.values = list(possible_values)

    def validate(self, value):
        if value not in self.values:
            raise ValidationError("Значение отсутствует в списке")
        return value


class Series:
    def __init__(self, validator=None, min_len=None, max_len=None, fixed_len=None):
        self.validator = validator
        self.min_len = min_len
        self.max_len = max_len
        self.fixed_len = fixed_len

    def validate(self, value):
        result = value.split(",") if isinstance(value, str) else list(value)
        if self.min_len is not None and len(result) < self.min_len or self.max_len is not None and len(result) > self.max_len or self.fixed_len is not None and len(result) != self.fixed_len:
            raise ValidationError("Некорректная длина списка")
        return [self.validator.validate(item) if self.validator else item for item in result]
