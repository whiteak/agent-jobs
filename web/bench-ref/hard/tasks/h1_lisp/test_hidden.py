import sys, unittest
from solution import run, LispError


class T(unittest.TestCase):
    def test_arith(self):
        self.assertEqual(run("(+ 1 2 3)"), 6)
        self.assertEqual(run("(- 10 3 2)"), 5)
        self.assertEqual(run("(- 4)"), -4)
        self.assertEqual(run("(+)"), 0)
        self.assertEqual(run("(*)"), 1)
        self.assertEqual(run("(quotient -7 2)"), -3)
        self.assertEqual(run("(remainder -7 2)"), -1)
        self.assertEqual(run("(quotient 7 -2)"), -3)
        self.assertEqual(run("(remainder 7 -2)"), 1)
        self.assertEqual(run("(< 1 2 3)"), True)
        self.assertEqual(run("(< 1 3 2)"), False)
        self.assertEqual(run("(= 2 2 2)"), True)

    def test_multi_and_comments(self):
        src = """
        ; a comment
        (define x 5) ; trailing
        (define (sq n) (* n n))
        (sq x)
        """
        self.assertEqual(run(src), 25)
        self.assertIsNone(run(""))
        self.assertIsNone(run("(define y 1)"))

    def test_truthiness_and_logic(self):
        self.assertEqual(run("(if 0 'yes 'no)"), "yes")
        self.assertEqual(run("(if '() 1 2)"), 1)
        self.assertEqual(run("(if #f 1 2)"), 2)
        self.assertEqual(run("(or #f 3)"), 3)
        self.assertEqual(run("(and 1 2 3)"), 3)
        self.assertEqual(run("(and 1 #f (car '()))"), False)
        self.assertEqual(run("(or)"), False)
        self.assertEqual(run("(and)"), True)

    def test_quote_lists(self):
        self.assertEqual(run("'(1 (2 a) #t)"), [1, [2, "a"], True])
        self.assertEqual(run("(cons 1 '(2 3))"), [1, 2, 3])
        self.assertEqual(run("(cdr '(1))"), [])
        self.assertEqual(run("(length (list 1 2 3))"), 3)
        self.assertEqual(run("(null? '())"), True)
        self.assertEqual(run("(apply + '(1 2 3))"), 6)
        self.assertEqual(run("(quote x)"), "x")

    def test_closures_and_set(self):
        src = """
        (define (make-counter)
          (let ((n 0))
            (lambda () (set! n (+ n 1)) n)))
        (define a (make-counter))
        (define b (make-counter))
        (a) (a) (b)
        (list (a) (b))
        """
        self.assertEqual(run(src), [3, 2])

    def test_let_parallel(self):
        self.assertEqual(run("(define x 1) (let ((x 2) (y x)) y)"), 1)
        self.assertEqual(run("(define x 1) (let ((x 10)) (set! x 5) x)"), 5)
        self.assertEqual(run("(define x 1) (let ((x 10)) (set! x 5)) x"), 1)

    def test_define_scope(self):
        src = """
        (define x 'global)
        (define (f) (define x 'local) x)
        (list (f) x)
        """
        self.assertEqual(run(src), ["local", "global"])

    def test_cond(self):
        src = """
        (define (sign n) (cond ((< n 0) 'neg) ((= n 0) 'zero) (else 'pos)))
        (list (sign -3) (sign 0) (sign 9))
        """
        self.assertEqual(run(src), ["neg", "zero", "pos"])
        self.assertIsNone(run("(cond (#f 1))"))

    def test_tail_calls(self):
        src = """
        (define (loop n acc) (if (= n 0) acc (loop (- n 1) (+ acc 1))))
        (loop 1000000 0)
        """
        self.assertEqual(run(src), 1000000)

    def test_tail_calls_mutual_and_forms(self):
        src = """
        (define (ev? n) (if (= n 0) #t (od? (- n 1))))
        (define (od? n) (cond ((= n 0) #f) (else (begin (ev? (- n 1))))))
        (ev? 300001)
        """
        self.assertEqual(run(src), False)
        src2 = """
        (define (f n) (and #t (or #f (let ((m (- n 1))) (if (= m 0) 'done (f m))))))
        (f 200000)
        """
        self.assertEqual(run(src2), "done")

    def test_deep_non_tail(self):
        src = "(define (sum n) (if (= n 0) 0 (+ n (sum (- n 1))))) (sum 500)"
        self.assertEqual(run(src), 125250)

    def test_errors(self):
        bad = ["(undefined-thing)", "(1 2)", "((lambda (x) x))", "(quotient 1 0)", "(car '())",
               "(+ 1 2", ")", "(if 1 2)", "(set! nope 1)", "(cdr 5)", "(remainder 3 0)"]
        for s in bad:
            with self.assertRaises(LispError, msg=s):
                run(s)


if __name__ == "__main__":
    sys.setrecursionlimit(10000)
    unittest.main()
