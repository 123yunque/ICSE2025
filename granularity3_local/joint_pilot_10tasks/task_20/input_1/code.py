
def is_woodall(x): 
	if not isinstance(x, int):
		return False
	if x <= 0 or x % 2 == 0:
		return False
	if (x == 1): 
		return True
	x += 1 
	i = 0
	while (x % 2 == 0): 
		x /= 2
		i += 1
		if (i == x): 
			return True
	return False
