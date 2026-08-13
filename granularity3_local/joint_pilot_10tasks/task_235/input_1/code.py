
def even_bit_set_number(n): 
    mask = 2
    while mask < n:
        n |= mask
        mask <<= 2
    return n
