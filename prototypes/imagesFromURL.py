import requests
import os
import json
from pprint import pprint

from bs4 import BeautifulSoup

def dump_image_files(url_list=None, folder_name=None, gallery_title=None):
	'''
	Given a list of urls (which can have 1 or more url elements), will dump all the images
	into a folder called /images/<gallery_title>/<folder_name>/, where <gallery_title> and
	<folder_name> are specified as parameters. 
	'''
	for url in url_list:
		response = requests.get(url, stream=True)
		if response.status_code == 200:	
			image_name = url.split('/')[-1].rstrip()								
			if gallery_title is not None: # If we supplied a gallery_title,
				full_path = os.path.join('images', gallery_title, folder_name, image_name) # Then use it
			else:				
				full_path = os.path.join('images', folder_name, image_name)
			if not os.path.exists(os.path.dirname(full_path)): # If the folder doesn't exist yet,
				os.makedirs(os.path.dirname(full_path)) # Create it
			if not os.path.isfile(full_path):
				with open(full_path, 'wb') as out_file: # Then write to it				
				    out_file.write(response.content)
			else:
				print image_name + ' already exists, ignoring.'

def retrieve_image_urls_from_source(source_file=None, class_name=None, bad_class_name=None, debug=False):
	'''
	Given an html or text document, will find all image urls (png and jpg only) and return them.
	'''	
	images = []
	html_file = ''
	with open(source_file, 'r') as infile:
		for line in infile.readlines():
			html_file += line
	soup = BeautifulSoup(html_file, 'html.parser')

	if debug:
		print soup.prettify()
	pass
	for link in soup.find_all('img'):
		if link.get('class') is not None and bad_class_name not in link.get('class') and class_name in link.get('class'):
			images.append(link.get('data-src'))
	
	image_urls = []
	for image in images:
		image_url = 'http:' + image
		image_urls.append(str(image_url))
	return image_urls


def get_source_images(source_file=None, folder_name=None, class_name=None, bad_class_name=None):
	'''
	Given an html or text document, will gather image urls and dump the image files.	
	'''
	image_urls = retrieve_image_urls_from_source(source_file=source_file, class_name=class_name, bad_class_name=bad_class_name)
	dump_image_files(url_list=image_urls, folder_name=folder_name)

def get_gallery_images(gallery_urls=None, gallery_title=None):
	'''
	Given a list of gallery urls, will gather image urls and dump the image files.
	'''
	def retrieve_image_urls_from_url(url=None, debug=False):
		'''
		Given a url, will find all image urls (png and jpg only) and return them.
		'''
		images = []
		html_file = requests.get(url)
		soup = BeautifulSoup(html_file.content, 'html.parser')
		
		if debug:
			print soup.prettify()

		for link in soup.find_all('a'):
			if '.jpg' in link.get('href') or '.png' in link.get('href'):
				images.append(link.get('href'))

		image_urls = []
		for image in images:
			if image[0] == '/': # If the image url is a relative link
				image_url = url+image # Add the original url as a prefix
			else:
				image_url = image
			image_urls.append(str(image_url))
		return image_urls

	url_list = gallery_urls # File with list of urls
	for url in url_list:
		original_url = url.rstrip() # Strip that nasty \n
		title = original_url.split('/')[-1].rstrip() # Strip that nasty \n		
		print 'Gallery URL: ' + original_url
		print 'Folder: ' + title + '\n'
		full_path = os.path.join('images', gallery_title, title)
		if os.path.exists(full_path):
			print 'Folder already exists, ignoring.\n'
		else:
			image_urls = retrieve_image_urls_from_url(url=original_url)
			dump_image_files(url_list=image_urls, folder_name=title, gallery_title=gallery_title)


def retrieve_gallery_urls(url=None, class_name=None, debug=False):
	'''
	Given a url, will find all gallery urls and return them.
	'''
	galleries = []	
	html_file = requests.get(url)	
	soup = BeautifulSoup(html_file.content, 'html.parser')

	if debug:
		print soup.prettify()

	for link in soup.find_all('a'):
		if link.get('class') is not None and class_name in link.get('class'):
			if 'galleries' in link.get('href'):
				galleries.append(link.get('href'))
	
	gallery_urls = []
	url = url.split('/')[2]	# ['http', '', 'www.website.com', 'page1']
	for gallery in galleries:
		if gallery[0] == '/': # If the image url is a relative link			
			gallery_url = 'http://'+url+gallery # Add the original url as a prefix
		else:
			gallery_url = gallery
		gallery_urls.append(str(gallery_url))

	return gallery_urls

def get_images_from_list(url_list='data/url_list.json'):
	'''
	Retrieve images from a list of individual urls.
	'''
	def retrieve_image_urls_from_url(url=None, debug=False):
		'''
		Given a url, will find all image urls (png and jpg only) and return them.
		'''
		images = []
		html_file = requests.get(url)
		soup = BeautifulSoup(html_file.content, 'html.parser')
		
		if debug:
			print soup.prettify()

		for link in soup.find_all('a'):
			if '.jpg' in link.get('href') or '.png' in link.get('href'):
				images.append(link.get('href'))

			image_urls = []
			for image in images:
				image_url = 'http:' + image
				image_urls.append(str(image_url))
			return image_urls

	with open(url_list) as infile:
		#pprint(json.load(infile))
		url_list = json.load(infile)
		for url in url_list['urls']:
			original_url = url['url']
			title = url['title']
			if requests.get(original_url).status_code == 200: # If we're working with a real url
				print '-------------------------------------------------------------------------------------'
				print 'Original URL: ' + original_url
				print '-------------------------------------------------------------------------------------'
				image_urls = retrieve_image_urls_from_url(url=original_url)
				print image_urls
				dump_image_files(url_list=image_urls, folder_name=title)

def get_galleries_from_list(url_list='data/gallery_urls.json'):
	'''
	Retrieve images from a list of gallery urls.
	'''
	with open(url_list) as infile:
		#pprint(json.load(infile))
		url_list = json.load(infile)
		for url in url_list['urls']:
			page = 1 # For multiple pages
			new_url = url['url']
			title = url['title']
			while requests.get(new_url).status_code == 200: # As long as we're working with a real url
				print '-------------------------------------------------------------------------------------'
				print 'Page URL: ' + new_url
				print '-------------------------------------------------------------------------------------'
				gallery_urls = retrieve_gallery_urls(url=new_url, class_name='track') # This class name signifies a gallery (in this case, 'track')
				get_gallery_images(gallery_urls=gallery_urls, gallery_title=title)
				page += 1 # Increment the page number
				new_url = url['url'] + '?page='+str(page) # Try the new page				

	# with open(url_list, 'r') as infile:
	# 	for url in infile.readlines():
	# 		page = 1 # For multiple pages
	# 		original_url = url.rstrip() # Strip that nasty \n		
	# 		title = original_url.split('/')[-1].rstrip() # Strip that nasty \n		
	# 		while requests.get(original_url).status_code == 200: # As long as we're working with a real url
	# 			gallery_urls = retrieve_gallery_urls(url=original_url, class_name='track') # This class name signifies a gallery (in this case, 'track')
	# 			get_gallery_images(gallery_urls=gallery_urls, gallery_title=title)
	# 			original_url += '?page='+str(page)



# Get the images from a source file:
# source_file = 'data/source.html'
# title = 'suicide-girls'
# class_name = 'unloaded' # The class we are searching for that contains images
# bad_class_name = 'thumb-title' # The class of unwanted images (likely thumbnails)
# get_source_images(source_file=source_file, folder_name=title, class_name=class_name, bad_class_name=bad_class_name)


# Get the images from a url list:
get_images_from_list()

# Get the gallery images from a url list:
#get_galleries_from_list()

