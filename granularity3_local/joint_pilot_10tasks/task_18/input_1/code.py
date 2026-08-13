
def remove_dirty_chars(string, second_string): 
	for char in second_string:
		string = string.replace(char, '')
	return string
