# inspired by https://github.com/rmarx/aioquic/tree/master/scripts

import os
import subprocess

# need to run setup.py first to make sure all our changes are compiled before running
# if you didn't make changes to aioquic, you can comment this step out
# need to run this from inside the root dir
# so in /srv/aioquic, do python3 scripts/run_tests.py

# print("Compiling...")
# process = subprocess.run("{}".format("python3 /srv/aioquic/setup.py install"), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

# if process.returncode != 0:
#     print ("ERROR in compilation: ", process.returncode, " != 0?")
#     print ( process.stderr )

# print("Compilation done!")

base_path = os.path.dirname(os.path.realpath(__file__))
# basecommand = f"python3 {os.path.join(base_path, '../examples/http3_client.py')} --insecure -v "
# basecommand = f"{os.path.join(base_path, '../.venv/bin/python')} {os.path.join(base_path, '../examples/http3_client.py')} --insecure -v "
basecommand = "python3 ./examples/http3_client.py --insecure -v"

class Endpoint:
    def __init__(self, name, urls):
        self.name = name
        self.urls = urls

# runname = ""

# most URls were discovered manually by visiting sites likely hosted on a given platform and looking for resources once confirmed
# others (r20-100KB.png) were taken from https://dev.neilnode.com/alienprobe-1.2.5.js which in turn were inspired by https://performance.radar.cloudflare.com/beacon.js

allendpoints = [
    Endpoint("Cloudflare", [
                            # "https://www.cloudflare.com/app-18cb3eb3ed5aedd2a5b1.js", # CF homepage, 1.4MB (Unavailable after 21/02/2024)
                            # "https://www.cloudflare.com/app-6e18c2731ac4d7e8f25d.js", # CF homepage, 1.4MB (1.35MB to be precise, stayed the same)
                            "https://www.cloudflare.com/app-54d134b22c9808b42dac.js", # CF homepage, 1.4MB (1.35MB to be precise, stayed the same)
                            "https://lucaspardue.com/wp-content/beagle-max-1.jpg", # Lucas Pardue direct, 1.5 MB (-1 to -10 available, all the same)
                            "https://lucaspardue.com/wp-content/art.jpg", # Lucas Pardue direct, 2.9 MB
                            # "https://ptcfc.com/img/284/r20-100KB.png" # 102KB
                            ]), 

    Endpoint("Akamai", [
                            "https://www.internetonmars.org/h3prios/server-tests/test.js", # Similar 1.3MB JS as Cloudflare, to keep results as consistent as possible (We have direct control over this Akamai deployment)
                            "https://cedexis-test.akamaized.net/img/r20-100KB.png", # 102KB, freeflow
                            # "https://essl-cdxs.edgekey.net/img/r20-100KB.png" # 102KB, ESSL (no H3 enabled yet though)
                        ]), 

    Endpoint("QUICCloud", ["https://www.quic.cloud/wp-content/litespeed/js/4719a4072c1a2469d85886b6a8e768ad.js?ver=b9fc0"]), # 366 KB, largest I could find
    Endpoint("Fastly", [
                            # "https://www.fastly.com/app-2256891624a3588fe57b.js", # Pricing page, 447 KB
                            "https://www.fastly.com/app-fe0b1187523c9c55a21e.js", # Pricing page, 447 KB
                            "https://fastly.cedexis-test.com/img/20367/r20-100KB.png" # 102KB
                        ]),

    Endpoint("AmazonCloudfront", [
                            "https://a.b.cdn.console.awsstatic.com/a/v1/C2LGMTKF7HUIMXMWTJOQPYZ4QQM6U7NBNAZLZEQRWULUVZAZFLVQ/module.js", # 1.1 MB
                            # "https://www.reezocar.com/_next/static/chunks/pages/_app-24e6c5fa4db04512.js", # homepage 3.1MB
                            "https://www.reezocar.com/_next/static/chunks/pages/_app-6415fd50ccc6b3bf.js", # homepage 3.1MB
                            "https://p29.cedexis-test.com/img/r20-100KB.png"
                        ]),
    Endpoint("GoogleCloudCDN", [
                            "https://cdn.hackersandslackers.com/2017/11/_retina/pandasmerge@2x.jpg", # 544 KB
                            "https://benchmark.1e100cdn.net/r20-100KB.png"
                        ]), 
    
    Endpoint("Caddy", [ 
                        "https://nodefalcon.com/img/art-collection-management.jpg", # 119 KB
                        "https://moebuta.org/posts/using-templates-with-caddy-and-hugo/go_integration_with_go_templates.animated_hu0048abfae485ab9ce34f494d8251d3d0_401071_720x0_resize_box_1.gif" # 2.1MB gif 
                      ]),

    # the bunny-CDN URLs work over HTTP/2 and advertise H3 via alt-svc, but they don't actually accept HTTP/3 connections... disable for now 
    Endpoint("jsdelivr", [
                        # "https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.css", # 281KB, unclear which CDN you'll hit, so use custom URLs below
                        "https://testingcf.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.css", # 281KB
                        "https://fastly.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.css", # 281KB    
                        # "https://jsdelivr.b-cdn.net/npm/bootstrap@5.3.2/dist/css/bootstrap.css", # 281KB                        
                        "https://testingcf.jsdelivr.net/gh/jimaek/testobjects@0.0.1/r20-100KB.png", # 102KB, cloudflare backend
                        "https://fastly.jsdelivr.net/gh/jimaek/testobjects@0.0.1/r20-100KB.png", # 102KB, fastly backend
                        # "https://jsdelivr.b-cdn.net/gh/jimaek/testobjects@0.0.1/r20-100KB.png" # 102KB, bunny CDN backend (weird alt-svc :O)
                        ]),
    Endpoint("gstatic", ["https://www.gstatic.com/devrel-devsite/prod/v89c3b644dadab0c1b29fcdfaa83db3f3db74c1887a83ba5a78318ee59aec3871/cloud/js/devsite_app_custom_elements_module.js"]), # 1.1 MB JS 

    Endpoint("Shopify", ["https://cdn.shopify.com/shopifycloud/brochure-iii/production/_assets/brochureV2-DT5SJUFK.css", # 468 KB 
                         "https://cdn.shopify.com/b/shopify-brochure2-assets/288aa2d76b4e7aaff082af1eb4279091.avif"])   # 599 KB animated AVIF
]

custom_endpoints = [
    Endpoint("nginx", ["https://h3.internetonmars.org:50035/test.js"]),
    Endpoint("caddy", ["https://h3.internetonmars.org:50098/test.js"]),
]


# endpoints = allendpoints 
# endpoints = custom_endpoints 
endpoints = allendpoints + custom_endpoints 
# endpoints = [allendpoints[1]] 



experiments = [
    "no-priority-instant",
    "no-priority-staggered",
    "u3-incremental-headers-instant",
    "u3-incremental-preframes-instant",
    "u3-incremental-preframes-100ms-instant",
    "u3-incremental-preframes-200ms-instant",
    "u3-incremental-postframes-instant",
    "u3-incremental-postframes-100ms-instant",
    "u3-incremental-postframes-200ms-instant",
    "u3-incremental-headers-staggered",
    "u3-incremental-preframes-staggered",
    "u3-incremental-preframes-100ms-staggered",
    "u3-incremental-preframes-200ms-staggered",
    "u3-incremental-postframes-staggered",
    "u3-incremental-postframes-100ms-staggered",
    "u3-incremental-postframes-200ms-staggered",

    "late-highprio-headers-instant",
    "late-highprio-preframes-instant",
    "late-highprio-preframes-100ms-instant",
    "late-highprio-preframes-200ms-instant",
    "late-highprio-postframes-instant",
    "late-highprio-postframes-100ms-instant",
    "late-highprio-postframes-200ms-instant",
    "late-highprio-headers-staggered",
    "late-highprio-preframes-staggered",
    "late-highprio-preframes-100ms-staggered",
    "late-highprio-preframes-200ms-staggered",
    "late-highprio-postframes-staggered",
    "late-highprio-postframes-100ms-staggered",
    "late-highprio-postframes-200ms-staggered",

    "late-highprio-incremental-headers-instant",
    "late-highprio-incremental-preframes-instant",
    "late-highprio-incremental-preframes-100ms-instant",
    "late-highprio-incremental-preframes-200ms-instant",
    "late-highprio-incremental-postframes-instant",
    "late-highprio-incremental-postframes-100ms-instant",
    "late-highprio-incremental-postframes-200ms-instant",
    "late-highprio-incremental-headers-staggered",
    "late-highprio-incremental-preframes-staggered",
    "late-highprio-incremental-preframes-100ms-staggered",
    "late-highprio-incremental-preframes-200ms-staggered",
    "late-highprio-incremental-postframes-staggered",
    "late-highprio-incremental-postframes-100ms-staggered",
    "late-highprio-incremental-postframes-200ms-staggered",

    "mixed-bucket-headers-instant",
    "mixed-bucket-preframes-instant",
    "mixed-bucket-preframes-100ms-instant",
    "mixed-bucket-preframes-200ms-instant",
    "mixed-bucket-postframes-instant",
    "mixed-bucket-postframes-100ms-instant",
    "mixed-bucket-postframes-200ms-instant",
    "mixed-bucket-headers-staggered",
    "mixed-bucket-preframes-staggered",
    "mixed-bucket-preframes-100ms-staggered",
    "mixed-bucket-preframes-200ms-staggered",
    "mixed-bucket-postframes-staggered",
    "mixed-bucket-postframes-100ms-staggered",
    "mixed-bucket-postframes-200ms-staggered",

    # "mixed-signals-preframes-instant",
    # "mixed-signals-preframes-100ms-instant",
    # "mixed-signals-preframes-200ms-instant",
    # "mixed-signals-postframes-instant",
    # "mixed-signals-postframes-100ms-instant",
    # "mixed-signals-postframes-200ms-instant",
    # "mixed-signals-preframes-staggered",
    # "mixed-signals-preframes-100ms-staggered",
    # "mixed-signals-preframes-200ms-staggered",
    # "mixed-signals-postframes-staggered",
    # "mixed-signals-postframes-100ms-staggered",
    # "mixed-signals-postframes-200ms-staggered",

    "reprioritization-50ms-headers-instant",
    "reprioritization-50ms-preframes-20ms-instant",
    "reprioritization-50ms-postframes-20ms-instant",
    "reprioritization-50ms-headers-staggered",
    "reprioritization-50ms-preframes-20ms-staggered",
    "reprioritization-50ms-postframes-20ms-staggered",
]

# handshake failure for some reason... seems to work with chrome though
# Endpoint("nginx", ["https://welcome.huddersfield.click/wp-content/themes/twentytwenty/assets/fonts/inter/Inter-upright-var.woff2"]),

class ExperimentException(Exception):
    pass

def run_command(cmd):
    process = subprocess.run("{}".format(cmd), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    if ( len(process.stdout) > 0 ):
        print ( process.stdout )

    if len(process.stderr) != 0 or process.returncode != 0:
        # print ("Potential ERROR in process: ", process.returncode, " != 0?")
        print ( process.stderr )

    if "ConnectionError" in process.stdout or "ConnectionError" in process.stderr:
        raise ExperimentException(f"'ConnectionError' found in stdout or stderr")

def run_experiments():
    # want to build a string as such: 
    # basecommand +  " --experiment no-headers-instant " + " --quic-log /srv/aioquic/qlog/no-headers-instant_CF4 " + "https://www.cloudflare.com/app-fb825b9f46d28bd11d98.js"
    potential_failures = []
    for experiment in experiments:
        for endpoint in endpoints:
            for urlindex, url in enumerate(endpoint.urls):
                cmd = basecommand + " --experiment " + experiment + " --quic-log ./qlog/" + experiment + "_" + endpoint.name + "_url" + str(urlindex) + " " + url
                print( "Running ", cmd )
                try:
                    run_command( cmd )
                except ExperimentException as e:
                    potential_failures.append(f"Experiment [{experiment}] - Endpoint [{url}] - {str(e)}")
    
    if len(potential_failures) > 0:
        print("Test ended with the following potential failures:")
        for f in potential_failures:
            print(f)


# TODO: run a basic experiment first to make sure the URLs are cached in the closest CDN edge node 
run_experiments()

# run_command( basecommand +  " --experiment no-headers-instant " + " --quic-log /srv/aioquic/qlog/Cloudflare " + "https://www.cloudflare.com/app-fb825b9f46d28bd11d98.js")
# run_command( basecommand +  " --experiment no-headers-instant " + " --quic-log /srv/aioquic/qlog/Akamai " + "https://www.internetonmars.org/posts/h2-server-push/images/7_bandwidthopportunities.png")
# run_command( basecommand +  " --experiment no-headers-instant " + " --quic-log /srv/aioquic/qlog/QUICCloud " + "https://www.quic.cloud/wp-content/litespeed/js/4719a4072c1a2469d85886b6a8e768ad.js?ver=b9fc0")

# run_command( basecommand +  " --experiment no-headers-instant " + " --quic-log /srv/aioquic/qlog/Caddy " + "https://nodefalcon.com/img/art-collection-management.jpg")


# run_command( basecommand +  " --experiment u3-incremental-instant " + " --quic-log /srv/aioquic/qlog/Cloudflare " + "https://www.cloudflare.com/app-fb825b9f46d28bd11d98.js")
# run_command( basecommand +  " --experiment u3-incremental-instant " + " --quic-log /srv/aioquic/qlog/Akamai " + "https://www.internetonmars.org/posts/h2-server-push/images/7_bandwidthopportunities.png")

# run_command( basecommand +  " --experiment u3-incremental-instant " + " --quic-log /srv/aioquic/qlog/QUICCloud " + "https://www.quic.cloud/wp-content/litespeed/js/4719a4072c1a2469d85886b6a8e768ad.js?ver=b9fc0")

# run_command( basecommand +  " --experiment late-highprio-delayed " + " --quic-log /srv/aioquic/qlog/Cloudflare " + "https://www.cloudflare.com/app-fb825b9f46d28bd11d98.js") 
# run_command( basecommand +  " --experiment late-highprio-delayed " + " --quic-log /srv/aioquic/qlog/Akamai " + "https://www.internetonmars.org/posts/h2-server-push/images/7_bandwidthopportunities.png")
# run_command( basecommand +  " --experiment late-highprio-delayed " + " --quic-log /srv/aioquic/qlog/QUICCloud " + "https://www.quic.cloud/wp-content/litespeed/js/4719a4072c1a2469d85886b6a8e768ad.js?ver=b9fc0")

# run_command( basecommand +  " --experiment late-highprio-instant " + " --quic-log /srv/aioquic/qlog/Cloudflare " + "https://www.cloudflare.com/app-fb825b9f46d28bd11d98.js") 
# run_command( basecommand +  " --experiment late-highprio-instant " + " --quic-log /srv/aioquic/qlog/Akamai " + "https://www.internetonmars.org/posts/h2-server-push/images/7_bandwidthopportunities.png")
# run_command( basecommand +  " --experiment late-highprio-instant " + " --quic-log /srv/aioquic/qlog/QUICCloud " + "https://www.quic.cloud/wp-content/litespeed/js/4719a4072c1a2469d85886b6a8e768ad.js?ver=b9fc0")

# run_command( basecommand +  " --experiment late-highprio-delayed " + " --quic-log /srv/aioquic/qlog/Caddy " + "https://moebuta.org/posts/using-templates-with-caddy-and-hugo/go_integration_with_go_templates.animated_hu0048abfae485ab9ce34f494d8251d3d0_401071_720x0_resize_box_1.gif")

# run_command( basecommand + " --quic-log /srv/aioquic/qlog/test_qlog_output2_akam_test4img.qlog " + "https://www.internetonmars.org/posts/h2-server-push/images/7_bandwidthopportunities.png https://www.internetonmars.org/posts/h2-server-push/images/7_bandwidthopportunities.png https://www.internetonmars.org/posts/h2-server-push/images/7_bandwidthopportunities.png https://www.internetonmars.org/posts/h2-server-push/images/7_bandwidthopportunities.png")


# run_command( basecommand +  " --experiment no-headers-instant " + " --quic-log /srv/aioquic/qlog/fastly " + "https://www.fastly.com/app-d3379d2cd2b112b78397.js")
# run_command( basecommand +  " --experiment late-highprio-delayed " + " --quic-log /srv/aioquic/qlog/fastly " + "https://www.fastly.com/app-d3379d2cd2b112b78397.js")


# run_command( basecommand +  " --experiment no-headers-instant " + " --quic-log /srv/aioquic/qlog/Shopify2 " + "https://cdn.shopify.com/shopifycloud/brochure-iii/production/_assets/brochureV2-DT5SJUFK.css")
# run_command( basecommand +  " --experiment late-highprio-delayed " + " --quic-log /srv/aioquic/qlog/Shopify2 " + "https://cdn.shopify.com/shopifycloud/brochure-iii/production/_assets/brochureV2-DT5SJUFK.css")


# run_command( basecommand +  " --experiment no-headers-instant " + " --quic-log /srv/aioquic/qlog/CF3 " + "https://lucaspardue.com/wp-content/uploads/2018/12/Anti-Matter-676x956.jpg")
# run_command( basecommand +  " --experiment late-highprio-delayed " + " --quic-log /srv/aioquic/qlog/CF3 " + "https://lucaspardue.com/wp-content/uploads/2018/12/Anti-Matter-676x956.jpg")
# run_command( basecommand +  " --experiment late-highprio-instant " + " --quic-log /srv/aioquic/qlog/CF3 " + "https://lucaspardue.com/wp-content/uploads/2018/12/Anti-Matter-676x956.jpg")

# run_command( basecommand +  " --experiment no-headers-instant " + " --quic-log /srv/aioquic/qlog/CF4 " + "https://www.cloudflare.com/app-fb825b9f46d28bd11d98.js")
# run_command( basecommand +  " --experiment late-highprio-delayed " + " --quic-log /srv/aioquic/qlog/CF4 " + "https://www.cloudflare.com/app-fb825b9f46d28bd11d98.js")
# run_command( basecommand +  " --experiment late-highprio-instant " + " --quic-log /srv/aioquic/qlog/CF4 " + "https://www.cloudflare.com/app-fb825b9f46d28bd11d98.js")