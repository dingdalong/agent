from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import binascii

key_str = "a_32_character_secret_key_here!!"  # 例如 "mysecretkey12345678901234567890"
key = key_str.encode('utf-8')  # 确保长度是 32 字节，如果不是可能需要填充或截断

#"data":"ac5de10d67316672601a2815765b38b66562332bc36f815ea1e7cff386bd17fb55ae8755d17511bbc7e7b6319d4bb544e2e7","iv":"c156dec024f5eb56b88b77a177b514f3","tag":"e542056c744c11d442287c7284f547bf","timestamp":1774346854492}
# 从抓包中提取的加密数据、IV 和 tag（十六进制字符串）
data_hex = "6e0e863a4b2a2b945737876f8d11fd3838b49d6d07cde94afb02064e57b1feee6decabedc3f389ccca2cdf00be54da54dad9ed5f998d5b48049bcccbc560a2bebbe8c904c9d99a2425cfa0cb1d3aea8701a085aa822434909de8da629d9002fd6423a8ee6707895a7dde59b2dfcee44080e1362f0d4faf0636c97c434d1915b11ed06de8d778ed50fa82a658cbdbc1e2773ef3c56fd3de077db0afc9c6d0186f05ca74bec0b9d75c2dde3c4716a362da307c2f"
iv_hex = "5e7480f6f5fbe785f0f67cb79ffaa9d2"
tag_hex = "a527bc7e45d7f05d553b4bc1c24f359a"

data = binascii.unhexlify(data_hex)
iv = binascii.unhexlify(iv_hex)
tag = binascii.unhexlify(tag_hex)

cipher = Cipher(algorithms.AES(key), modes.GCM(iv, tag), backend=default_backend())
decryptor = cipher.decryptor()
plaintext = decryptor.update(data) + decryptor.finalize()
print(plaintext.decode('utf-8'))
