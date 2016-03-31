imagesFromURLpy is an image scraper that can scrape a URL or list of URLs which are populated by images or galleries of images.

# Reading URLs with images.
imagesFromURLpy can also read a list of URLs that simply have images on them. Name this file **url_list.json**. All JSON files should be kept in the **data** folder.

#JSON Format:

{
    "urls":
    [
    {
        "title": "title goes here",
        "url": "http://www.website.com/something"
    },
    {
        "title": "2nd title",
        "url": "http://www1.website.com/something2"
    }
    ]
}

When you have created the JSON file, you can dump all the images by using the **get_images_from_list(url_list)** method. The default **url_list** is *data/url_list.json*, but you can adjust that to whatever you want (as long as it's .json).


# Reading URLs with galleries.
You can create a json object of the following form to scrape a bunch of URLs. Use the form above. Name this file **gallery_urls.json**. All JSON files should be kept in the **data** folder.

These URLs need to be properly formatted (but you can also adjust the code to adhere to other formats).

# URL Format for a gallery URL: 

http://www.website.com/something

# Each gallery must have the following format:

http://www.website.com/something/galleries/<gallery_name>, where <gallery_name> can be any string

When you have created the JSON file, you can dump all the images by using the **get_galleries_from_list(url_list)** method. The default **url_list** is *data/gallery_urls.json*, but you can adjust that to whatever you want (as long as it's .json).

# Reading source code html or text documents
You can create a .html file or .txt file with html code in it (by copying the source of a particular site with image links that you want to scrape). Save the file as whatever name you choose. 

Once you have created the .html or .txt file, you can dump all the images by using the **get_source_images(source_file, folder_name, class_name, bad_class_name)** method. 