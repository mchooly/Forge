# -*- coding: utf-8 -*-
"""PHP 序列化结构 DSL —— 用户写「不带长度」的结构树，工具补齐每一个长度和计数。

存在的理由：PHP 的序列化格式里，**每一段都有一个字节长度前缀**，容器还要再声明
元素个数。手写时这两样都是静默错——长度差一个字节，`unserialize()` 直接
`Error at offset N`；`O:3:"Foo":2:{...}` 里声明两个属性却只写一个，也一样。
私有属性还要写成 `\\0类名\\0属性`，长度得把两个 NUL 一起算进去，肉眼几乎数不对。

而 POP 链的**结构**取决于目标的类：类名、属性名、可见性、嵌套形状，这些只能
由使用者提供。所以分工是——**结构你写，长度工具算**。

    O:App\\Cache{socket=O:SoapClient{uri=s:"http://x" location=s:"http://y"}}

展开成：

    O:9:"App\\Cache":1:{s:6:"socket";O:10:"SoapClient":2:{s:3:"uri";s:8:"http://x";s:8:"location";s:8:"http://y";}}

## 语法

值（空白随便加）：

| 写法 | 展开成 | 说明 |
| --- | --- | --- |
| `N` | `N;` | null |
| `b:1` / `b:0` | `b:1;` / `b:0;` | 布尔 |
| `i:123` | `i:123;` | 整数 |
| `d:1.5` | `d:1.5;` | 浮点 |
| `s:"文本"` | `s:<字节数>:"文本";` | 字符串，长度是 **UTF-8 字节数** |
| `a{...}` | `a:<项数>:{...}` | 数组 |
| `O:类名{...}` | `O:<类名字节数>:"类名":<项数>:{...}` | 对象 |

项之间用空白分隔，键后面可以跟一个 `=` 或 `:`——纯粹为了好读，工具会忽略它：

| 键 | 用于 | 说明 |
| --- | --- | --- |
| `i:0` | 数组 | 整数键 |
| `s:"k"` | 数组 | 字符串键 |
| `name` | 对象 | public 属性 |
| `-name` | 对象 | protected，展开成 `\\0*\\0name` |
| `#name` | 对象 | private，展开成 `\\0<外层类名>\\0name` |

字符串内容里 `\\"` 是转义的双引号、`\\\\` 是转义的反斜杠；其余字符原样收下，
包括 `;` `}`——**长度前缀就是为了让内容可以原样出现**（PHP 自己不转义引号，
`serialize('a"b')` 得到的正是 `s:3:"a"b";`，所以解析靠长度而不是找引号）。

## 边界

- **不做引用（`R:` / `r:`）**。它要维护 PHP 内部那份「第几个值」的编号，而那份编号
  的算法（键算不算一个槽位）我没有可靠出处，做错了就是静默错——需要引用的链
  （少数 Laravel gadget）请用 phpggc 生成后当变量贴进来。
- 不做 `E:`（enum，PHP 8.1）、不做 `C:`（Serializable 自定义格式）。
- **只生成不解析**——解析（校验）在 `forge/syntax.py` 里，两处独立实现。
  这是有意的：生成与校验共用一份代码的话，同一个理解错误会在两边同时成立，
  校验就成了摆设。
"""

import re

# 嵌套深度上限。POP 链一般不超过 10 层，给 64 足够；
# 没有上限的话一个手滑的 `O:A{O:B{O:C{` 会把 Python 的栈打爆，
# 而这是跑在用户机器上的代码。
MAX_DEPTH = 64

# 展开结果的长度上限（字节）。挡的是往 `s:"..."` 里粘一个巨大的东西。
MAX_OUT = 1 << 20

_WS = " \t\r\n"


class SpecError(ValueError):
    """结构描述本身有问题。调用方应当**原样透传**并把它当提示报出来。"""


class _Parser:
    def __init__(self, s):
        self.s = s
        self.i = 0
        self.n = len(s)

    # ---------------------------------------------------------------- 基础

    def err(self, msg):
        raise SpecError("%s（第 %d 个字符处：%r）"
                        % (msg, self.i, self.s[self.i:self.i + 12]))

    def skip_ws(self):
        while self.i < self.n and self.s[self.i] in _WS:
            self.i += 1

    def peek(self):
        return self.s[self.i] if self.i < self.n else ""

    def take(self, lit):
        if not self.s.startswith(lit, self.i):
            self.err("这里要 %r" % lit)
        self.i += len(lit)

    def number(self):
        m = re.match(r"-?\d+(?:\.\d+)?", self.s[self.i:])
        if not m:
            self.err("这里要一个数字")
        self.i += len(m.group(0))
        return m.group(0)

    def ident(self, what):
        """读一个标识符（类名或属性名）。

        终止符包含 `=`/`:`/`{`/`}` 和空白——前两个是「键后面那个纯装饰的分隔符」，
        不排除的话 `name=x` 会被整个读成标识符。
        """
        j = self.i
        while j < self.n and self.s[j] not in _WS + "{}:=#":
            j += 1
        if j == self.i:
            self.err("这里要一个%s" % what)
        got = self.s[self.i:j]
        self.i = j
        return got

    def quoted(self):
        """读一段双引号字符串，返回**解码后**的内容。

        `\\"` 是这个 DSL 自己的一层转义，与 PHP 无关——DSL 需要有个办法把引号
        写进内容里，而展开出去以后 PHP 那边靠长度前缀定位，内容里出现引号是合法的。
        不认识的转义（如命名空间里的 `\\`）原样保留。
        """
        self.take('"')
        buf = []
        while True:
            if self.i >= self.n:
                self.err("字符串没有收尾的双引号")
            c = self.s[self.i]
            if c == "\\":
                nxt = self.s[self.i + 1:self.i + 2]
                if nxt in ('"', "\\"):
                    buf.append(nxt)
                    self.i += 2
                    continue
                buf.append(c)
                self.i += 1
                continue
            if c == '"':
                self.i += 1
                return "".join(buf)
            buf.append(c)
            self.i += 1

    @staticmethod
    def slen(text):
        """**字节**数，不是字符数——和 `<<len()>>` 同一个口径。"""
        return len(text.encode("utf-8"))

    # ---------------------------------------------------------------- 值

    def value(self, depth, cls=None):
        if depth > MAX_DEPTH:
            self.err("嵌套超过 %d 层" % MAX_DEPTH)
        self.skip_ws()
        c = self.peek()
        if c == "":
            self.err("这里要一个值")
        if c == "N":
            self.i += 1
            return "N;"
        if c == "b":
            self.take("b:")
            self.skip_ws()
            v = self.number()
            if v not in ("0", "1"):
                self.err("布尔只能是 b:0 或 b:1")
            return "b:%s;" % v
        if c == "i":
            self.take("i:")
            self.skip_ws()
            return "i:%s;" % self.number()
        if c == "d":
            self.take("d:")
            self.skip_ws()
            return "d:%s;" % self.number()
        if c == "s":
            self.take("s:")
            self.skip_ws()
            text = self.quoted()
            return 's:%d:"%s";' % (self.slen(text), text)
        if c == "a":
            self.i += 1
            self.skip_ws()
            return self.container(depth, is_array=True)
        if c == "O":
            self.take("O:")
            self.skip_ws()
            name = self.ident("类名")
            self.skip_ws()
            return self.container(depth, is_array=False, cls=name)
        self.err("认不出的值（只支持 N / b: / i: / d: / s: / a{ / O:类名{）")

    def container(self, depth, is_array, cls=None):
        self.take("{")
        items = []
        while True:
            self.skip_ws()
            if self.peek() == "}":
                break
            if self.peek() == "":
                self.err("容器没有收尾的 }")
            items.append(self.item(depth + 1, is_array, cls))
        self.take("}")
        body = "".join(items)
        if is_array:
            return "a:%d:{%s}" % (len(items), body)
        return 'O:%d:"%s":%d:{%s}' % (self.slen(cls), cls, len(items), body)

    def item(self, depth, is_array, cls):
        if is_array:
            c = self.peek()
            if c == "i":
                self.take("i:")
                self.skip_ws()
                key = "i:%s;" % self.number()
            elif c == "s":
                self.take("s:")
                self.skip_ws()
                k = self.quoted()
                key = 's:%d:"%s";' % (self.slen(k), k)
            else:
                self.err('数组的键要写成 i:0 或 s:"名字"')
        else:
            sigil = ""
            if self.peek() in "-#":
                sigil = self.peek()
                self.i += 1
            name = self.ident("属性名")
            if sigil == "-":
                name = "\0*\0" + name
            elif sigil == "#":
                if "/" in name:
                    # `#类名/属性` —— **显式指定 mangling 用的那个类名**。
                    # 需要它是因为私有属性的 mangled 名用的是「声明该属性的类」，
                    # 而外层 O: 写的是子类。Guzzle 那条链就是这种形状：
                    # 属性 cookies 声明在父类 CookieJar 上，对象却是 FileCookieJar。
                    # 分隔符取 `/`：PHP 标识符里不出现它，而命名空间用的是反斜杠，
                    # 所以不会和二义的类名/属性名撞上。
                    mangler, _, name = name.partition("/")
                    if not mangler or not name:
                        self.err("# 后面的写法是 类名/属性名")
                    name = "\0%s\0%s" % (mangler, name)
                else:
                    if not cls:
                        self.err("private 属性只能写在 O:类名{...} 里")
                    name = "\0%s\0%s" % (cls, name)
            key = 's:%d:"%s";' % (self.slen(name), name)

        self.skip_ws()
        if self.peek() in "=:":     # 装饰性的，只为好读
            self.i += 1
        return key + self.value(depth, cls)


def build(spec):
    """把结构描述展开成 PHP 序列化串。结构有问题时抛 `SpecError`。"""
    p = _Parser(spec)
    out = p.value(0)
    p.skip_ws()
    if p.i != len(p.s):
        p.err("结构描述解析完了还有多余内容")
    if len(out.encode("utf-8")) > MAX_OUT:
        raise SpecError("展开结果超过 %d 字节，多半是哪里写错了" % MAX_OUT)
    return out
