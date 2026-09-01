// fix_dbtags.go — AST 级 db tags 迁移（EXECUTION 447）
package main

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
)

var BQ = string(rune(96))

func toSnake(n string) string {
	var out []rune
	rs := []rune(n)
	for i, ch := range rs {
		if ch >= 'A' && ch <= 'Z' && i > 0 {
			out = append(out, '_')
		}
		if ch >= 'A' && ch <= 'Z' {
			ch = ch + 32
		}
		out = append(out, ch)
	}
	return string(out)
}

type edit struct {
	line int
	new  string
}

func processFile(path string) (int, error) {
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, nil, parser.ParseComments)
	if err != nil {
		return 0, err
	}
	src, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	srcLines := strings.Split(string(src), "\n")
	var edits []edit
	if debug {
		fmt.Println("PROC", path)
	}
	ast.Inspect(f, func(n ast.Node) bool {
		st, ok := n.(*ast.StructType)
		if !ok {
			return true
		}
		for _, field := range st.Fields.List {
			if field.Tag == nil {
				continue
			}
			tagLit := field.Tag.Value
			if debug {
				fmt.Println("  FIELD", fieldName0(field), "TAG", tagLit)
			}
			if strings.Contains(tagLit, BQ+"db:") || !strings.Contains(tagLit, "gorm:") {
				continue
			}
			if len(field.Names) == 0 {
				continue
			}
			fieldName := field.Names[0].Name
			col := toSnake(fieldName)
			jmark := BQ + "json:" + QT
			if jm := strings.Index(tagLit, jmark); jm >= 0 {
				rest := tagLit[jm+len(jmark):]
				if e2 := strings.Index(rest, QT); e2 >= 0 {
					col = rest[:e2]
				}
			}
			inner := strings.Trim(tagLit, BQ)
			pos := fset.Position(field.Tag.Pos())
			lineIdx := pos.Line - 1
			if lineIdx < 0 || lineIdx >= len(srcLines) {
				continue
			}
			line := srcLines[lineIdx]
			i1 := strings.Index(line, BQ)
			i2 := strings.LastIndex(line, BQ)
			if i1 < 0 || i2 <= i1 {
				continue
			}
			newTag := "db:" + QT + col + QT + " " + inner
			newLine := line[:i1] + BQ + newTag + BQ + line[i2+1:]
			edits = append(edits, edit{line: lineIdx, new: newLine})
		}
		return true
	})
	if debug {
		fmt.Println("  EDITS COLLECTED:", len(edits))
	}
	applied := map[int]bool{}
	count := 0
	for i := len(edits) - 1; i >= 0; i-- {
		ed := edits[i]
		if applied[ed.line] {
			continue
		}
		srcLines[ed.line] = ed.new
		applied[ed.line] = true
		count++
	}
	if count == 0 {
		return 0, nil
	}
	return count, os.WriteFile(path, []byte(strings.Join(srcLines, "\n")), 0644)
}

var QT = string(rune(34))
var totalFixed int
var debug = len(os.Getenv("DBTAG_DEBUG")) > 0

func fieldName0(f *ast.Field) string {
	if len(f.Names) > 0 {
		return f.Names[0].Name
	}
	return "?"
}

var PKGS = []string{
	"billing", "device", "inventory", "location", "oms", "oms_rule",
	"outbound", "picking", "stocktake", "transaction", "transfer",
}

func main() {
	base := "internal"
	if len(os.Args) > 1 {
		base = os.Args[1]
	}
	for _, pkg := range PKGS {
		files, _ := filepath.Glob(filepath.Join(base, pkg, "*.go"))
		for _, f := range files {
			if strings.HasSuffix(f, "_test.go") {
				continue
			}
			fixed, err := processFile(f)
			if err != nil {
				fmt.Println("ERR", f, err)
				continue
			}
			totalFixed += fixed
			if fixed > 0 {
				fmt.Printf("%s: %d fields\n", f, fixed)
			}
		}
	}
	fmt.Println("TOTAL:", totalFixed)
}
