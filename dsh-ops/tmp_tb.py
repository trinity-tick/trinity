import io
p = r"D:\trinity-code\scripts\with_lease.py"
c = io.open(p, encoding="utf-8").read()
old = "if __name__ == "__main__":
    sys.exit(main())"
new = "if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as _exc:
        import traceback
        print("with_lease FATAL:", repr(_exc))
        traceback.print_exc()
        sys.exit(2)"
assert old in c
c = c.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="\n").write(c)
print("traceback added")
