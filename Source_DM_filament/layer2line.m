function Li = layer2line(La)
% La = nx1 vector in layer num
% Li = nx2 vector [startline, endline]

global Linfo

n = size(La,1);
Li = zeros(n,2);

Li(:,2) = Linfo(La);
for i=1:n
    if La(i) == 1
        Li(i,1) = 1;
    else
        Li(i,1) = Linfo(La(i)-1)+1;
    end
end
end
